#!/usr/bin/env node
/**
 * 恒剑知识库 —— 双仓库双向同步引擎
 *
 *   主仓库 (origin) : Gitee  https://gitee.com/ningbohengjianguangdian/hengjian-knowledge-hub.git
 *   镜像仓库 (mirror): GitHub https://github.com/futianyu760118-art/hjzsk.git
 *
 * 同步语义：
 *   1. 本地未推送的提交  -> 优先发布到 Gitee（主）
 *   2. Gitee 领先        -> 快进推送到 GitHub（镜像）
 *   3. GitHub 独有提交   -> 回灌合并进 Gitee
 *   4. 两端分叉          -> 合并后同时发布，冲突一律以 Gitee 内容为准
 *
 * 冲突策略：所有合并均以「主仓库侧」为 ours 执行 `git merge -X ours`，
 *          即 Gitee 优先；仅当 GitHub 侧为纯增量（无冲突）时才吸收其内容。
 *
 * 用法：
 *   node scripts/sync.js            # 正常同步
 *   node scripts/sync.js --dry-run   # 只推演不落盘
 *   node scripts/sync.js --status    # 只查看两端差异状态
 */

'use strict';

const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const REPO_ROOT = path.resolve(__dirname, '..');
const BRANCH = process.env.SYNC_BRANCH || 'main';
const PRIMARY = process.env.SYNC_PRIMARY || 'origin';
const MIRROR = process.env.SYNC_MIRROR || 'mirror';
const PRIMARY_LABEL = 'Gitee (主)';
const MIRROR_LABEL = 'GitHub (辅)';

const argv = process.argv.slice(2);
const DRY_RUN = argv.includes('--dry-run');
const STATUS_ONLY = argv.includes('--status');

const GIT_TIMEOUT = 120000;
const MAX_RETRY = 5;          // 网络类错误重试次数
const RETRY_BASE_MS = 2000;   // 重试退避基数（指数增长）
let logFile = null;

/* ────────────────────────── 基础设施 ────────────────────────── */

function log(msg, level = 'INFO') {
  const line = `[${new Date().toISOString()}] [${level}] ${msg}`;
  console.log(line);
  if (logFile) {
    try {
      fs.appendFileSync(logFile, line + '\n', 'utf8');
    } catch (_) { /* 日志写入失败不阻断主流程 */ }
  }
}

/**
 * 判断错误是否属于网络/代理抖动（可重试）。
 * 这类错误在代理隧道复用失败时非常常见。
 */
function isRetryable(errText) {
  const t = (errText || '').toLowerCase();
  const patterns = [
    'connect tunnel failed',
    'empty reply from server',
    'server closed abruptly',
    'missing close_notify',
    'connection reset',
    'connection timed out',
    'timed out',
    'could not resolve host',
    'temporary failure in name resolution',
    'the remote end hung up',
    'early eof',
    'rpc failed',
    '502',
    '503',
    '504',
    'bad gateway',
    'service unavailable',
  ];
  return patterns.some((p) => t.includes(p));
}

function sleep(ms) {
  // 同步阻塞等待，仅用于重试退避
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

function runGit(args) {
  try {
    const out = execFileSync('git', args, {
      cwd: REPO_ROOT,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
      timeout: GIT_TIMEOUT,
      env: { ...process.env, GIT_TERMINAL_PROMPT: '0', GIT_PAGER: 'cat' },
    });
    return { ok: true, code: 0, out: out || '', err: '' };
  } catch (e) {
    return {
      ok: false,
      code: typeof e.status === 'number' ? e.status : 1,
      out: e.stdout ? e.stdout.toString() : '',
      err: e.stderr ? e.stderr.toString() : (e.message || String(e)),
    };
  }
}

/**
 * 执行 git 命令，网络类错误自动重试（指数退避）。
 * 非网络错误（如合并冲突）立即返回，不做无谓重试。
 */
function git(args) {
  let last = null;
  for (let attempt = 1; attempt <= MAX_RETRY; attempt++) {
    last = runGit(args);
    if (last.ok) return last;

    if (!isRetryable(last.err)) return last;
    if (attempt === MAX_RETRY) {
      log(`git ${args.join(' ')} 重试 ${MAX_RETRY} 次仍失败: ${last.err.trim().split('\n')[0]}`, 'ERROR');
      return last;
    }
    const wait = RETRY_BASE_MS * Math.pow(2, attempt - 1);
    log(`网络异常(${last.err.trim().split('\n')[0]})，${Math.round(wait / 1000)}s 后重试 [${attempt}/${MAX_RETRY}]`, 'WARN');
    sleep(wait);
  }
  return last;
}

/**
 * 拉取远端分支。网络抖动时重试，最终失败返回 false（不中断同步流程）。
 */
function fetchRemote(remote) {
  const r = git(['fetch', remote, BRANCH]);
  if (r.ok) {
    log(`已拉取 ${remote}/${BRANCH}`);
    return true;
  }
  log(`拉取 ${remote}/${BRANCH} 失败: ${r.err.trim().split('\n')[0]}`, 'WARN');
  return false;
}

function revParse(ref) {
  const r = git(['rev-parse', '--verify', `${ref}^{commit}`]);
  return r.ok ? r.out.trim() : null;
}

function isAncestor(maybeAncestor, descendant) {
  if (!maybeAncestor || !descendant) return false;
  return git(['merge-base', '--is-ancestor', maybeAncestor, descendant]).ok;
}

function branchExists(ref) {
  return git(['show-ref', '--verify', '--quiet', `refs/remotes/${ref}`]).ok;
}

function short(sha) {
  return sha ? sha.slice(0, 8) : '--------';
}

/* ────────────────────────── 同步主流程 ────────────────────────── */

/**
 * 阶段一：让本地 HEAD 与主仓库(Gitee)对齐，并把本地未推送的工作发布到 Gitee。
 * 返回对齐后的 commit。
 */
function alignLocalToPrimary() {
  const O = revParse(`${PRIMARY}/${BRANCH}`);
  const L = revParse('HEAD');

  if (!O) {
    log(`主仓库分支 ${PRIMARY}/${BRANCH} 不存在，本地内容将作为首个提交发布`, 'WARN');
    push(`${PRIMARY}`, `${BRANCH}:${BRANCH}`);
    return revParse('HEAD');
  }
  if (O === L) {
    log(`本地与${PRIMARY_LABEL}已一致 (${short(L)})`);
    return L;
  }

  if (isAncestor(O, L)) {
    // 本地领先主仓库 -> 发布本地提交到 Gitee
    log(`本地领先${PRIMARY_LABEL}，发布本地提交到主仓库`);
    const r = push(`${PRIMARY}`, `${BRANCH}:${BRANCH}`);
    if (!r.ok) throw new Error('发布本地提交到主仓库失败');
    return revParse('HEAD');
  }

  if (isAncestor(L, O)) {
    // 主仓库领先本地 -> 快进本地
    log(`主仓库领先，本地快进到 ${short(O)}`);
    const r = git(['merge', '--ff-only', `${PRIMARY}/${BRANCH}`]);
    if (!r.ok) throw new Error(`本地快进失败: ${r.err.trim()}`);
    return revParse('HEAD');
  }

  // 分叉：以 Gitee 为 ours 合并本地提交，冲突保留 Gitee 内容
  log('本地与主仓库分叉，执行「主仓库优先」合并');
  if (DRY_RUN) return L;

  git(['branch', '-f', '__sync_tmp__', `${PRIMARY}/${BRANCH}`]);
  git(['checkout', '__sync_tmp__']);
  const m = git(['merge', '--no-edit', '-X', 'ours', L]);
  if (!m.ok) {
    git(['merge', '--abort']);
    git(['checkout', BRANCH]);
    git(['branch', '-D', '__sync_tmp__']);
    throw new Error('与主仓库合并失败，已回滚: ' + m.err.trim().split('\n')[0]);
  }
  const R = revParse('HEAD');
  git(['branch', '-f', BRANCH, R]);
  git(['checkout', BRANCH]);
  git(['branch', '-D', '__sync_tmp__']);
  log(`合并完成，结果提交 ${short(R)}`);
  push(`${PRIMARY}`, `${BRANCH}:${BRANCH}`);
  return R;
}

/**
 * 阶段二：把镜像仓库(GitHub)的独有提交回灌到主仓库，并发布到两端。
 */
function mirrorToPrimary(aligned) {
  if (!branchExists(`${MIRROR}/${BRANCH}`)) {
    log(`镜像仓库尚无分支 ${MIRROR}/${BRANCH}，无需回灌`);
    return { merged: false, result: aligned };
  }

  const M = revParse(`${MIRROR}/${BRANCH}`);
  const L = revParse('HEAD');

  if (M === L) {
    log(`两端已完全一致 (${short(L)})`);
    return { merged: false, result: L };
  }

  if (isAncestor(M, L)) {
    log(`${PRIMARY_LABEL}领先${MIRROR_LABEL}，快进推送到镜像仓库`);
    if (!DRY_RUN) {
      const r = push(`${MIRROR}`, `${BRANCH}:${BRANCH}`);
      if (!r.ok) throw new Error('推送到镜像仓库失败: ' + r.err.trim());
    }
    return { merged: false, result: L };
  }

  if (isAncestor(L, M)) {
    // 镜像仓库独有提交，主仓库无分叉 -> 直接快进主仓库到镜像
    log(`${MIRROR_LABEL}领先且无分叉，主仓库快进到 ${short(M)}`);
    if (DRY_RUN) return { merged: false, result: L };
    const r = git(['merge', '--ff-only', `${MIRROR}/${BRANCH}`]);
    if (!r.ok) throw new Error('主仓库快进失败: ' + r.err.trim());
    const R = revParse('HEAD');
    push(`${PRIMARY}`, `${BRANCH}:${BRANCH}`);
    push(`${MIRROR}`, `${BRANCH}:${BRANCH}`);
    return { merged: false, result: R };
  }

  // 两端分叉 -> 合并，冲突以 Gitee 为准
  log('两端分叉，执行合并（冲突以 Gitee 内容为准）');
  if (DRY_RUN) return { merged: false, result: L };

  const m = git(['merge', '--no-edit', '-X', 'ours', `${MIRROR}/${BRANCH}`]);
  if (!m.ok) {
    git(['merge', '--abort']);
    throw new Error('两端合并失败，已回滚: ' + m.err.trim().split('\n')[0]);
  }
  const R = revParse('HEAD');
  log(`合并完成，结果提交 ${short(R)}`);
  push(`${PRIMARY}`, `${BRANCH}:${BRANCH}`);
  push(`${MIRROR}`, `${BRANCH}:${BRANCH}`);
  return { merged: true, result: R };
}

function push(remote, refspec) {
  if (DRY_RUN) {
    log(`[dry-run] git push ${remote} ${refspec}`);
    return { ok: true };
  }
  const r = git(['push', remote, refspec]);
  if (!r.ok) {
    log(`推送 ${remote} 失败: ${r.err.trim()}`, 'ERROR');
  } else {
    log(`已推送到 ${remote}: ${r.out.trim().split('\n').filter(Boolean).pop() || 'ok'}`);
  }
  return r;
}

/* ────────────────────────── 状态快照 ────────────────────────── */

function statusSnapshot() {
  const O = revParse(`${PRIMARY}/${BRANCH}`);
  const M = revParse(`${MIRROR}/${BRANCH}`);
  const L = revParse('HEAD');
  let relation = '未知';
  if (O && M) {
    if (O === M) relation = '一致';
    else if (isAncestor(O, M)) relation = `${PRIMARY_LABEL}落后`;
    else if (isAncestor(M, O)) relation = `${PRIMARY_LABEL}领先`;
    else relation = '分叉';
  }
  return { O, M, L, relation };
}

function printStatus() {
  const s = statusSnapshot();
  const rows = [
    ['本地 HEAD', short(s.L)],
    [`${PRIMARY_LABEL} (${PRIMARY}/${BRANCH})`, short(s.O)],
    [`${MIRROR_LABEL} (${MIRROR}/${BRANCH})`, short(s.M)],
    ['两端关系', s.relation],
  ];
  console.log('\n恒剑知识库 · 同步状态');
  console.log('─'.repeat(46));
  for (const [k, v] of rows) console.log(`  ${k.padEnd(32, ' ')} ${v}`);
  console.log('─'.repeat(46));
  const dirty = git(['status', '--porcelain']).out.trim();
  console.log(dirty ? `  工作区有未提交改动:\n${dirty.split('\n').map(l => '    ' + l).join('\n')}` : '  工作区干净');
  console.log('');
}

/* ────────────────────────── 入口 ────────────────────────── */

function main() {
  const logDir = path.join(REPO_ROOT, 'logs');
  if (!DRY_RUN && fs.existsSync(logDir)) {
    const d = new Date().toISOString().slice(0, 10);
    logFile = path.join(logDir, `sync-${d}.log`);
  }

  log('════ 恒剑知识库双向同步开始 ════');
  log(`主仓库=${PRIMARY_LABEL}  镜像仓库=${MIRROR_LABEL}  分支=${BRANCH}${DRY_RUN ? '  [dry-run]' : ''}`);

  if (STATUS_ONLY) {
    printStatus();
    return 0;
  }

  // 保证当前在目标分支
  const cur = git(['rev-parse', '--abbrev-ref', 'HEAD']).out.trim();
  if (cur !== BRANCH) {
    const exists = git(['show-ref', '--verify', '--quiet', `refs/heads/${BRANCH}`]).ok;
    if (exists) git(['checkout', BRANCH]);
    else git(['checkout', '-b', BRANCH]);
    log(`切换到分支 ${BRANCH}`);
  }

  // 暂存未提交改动，避免影响合并
  let stashed = false;
  const dirty = git(['status', '--porcelain']).out.trim();
  if (dirty) {
    log('检测到未提交改动，已临时暂存');
    git(['stash', 'push', '--include-untracked', '-m', `autostash-${Date.now()}`]);
    stashed = true;
  }

  try {
    // 先拉取两端，否则无法感知远端的新提交
    fetchRemote(PRIMARY);
    fetchRemote(MIRROR);

    const aligned = alignLocalToPrimary();
    const outcome = mirrorToPrimary(aligned);

    log('════ 同步结束 ════');
    const s = statusSnapshot();
    log(`最终状态: 主=${short(s.O)} 镜像=${short(s.M)} 关系=${s.relation}`);
    if (outcome.merged) log('本次执行了跨端合并');
    else log('本次无跨端合并（快进或已一致）');

    if (stashed) {
      git(['stash', 'pop']);
      log('已恢复本地未提交改动');
    }
    return 0;
  } catch (e) {
    log('同步失败: ' + e.message, 'ERROR');
    if (stashed) {
      git(['stash', 'pop']);
      log('已恢复本地未提交改动');
    }
    return 1;
  }
}

process.exit(main());
