# 同步机制说明

## 一、整体结构

```
        ┌──────────────────────────┐
        │   本地工作副本            │
        │   C:\Users\colorful\WorkBuddy\
        │     hengjian-knowledge-hub │
        └────────┬────────────────┬┘
                 │                │
       origin ───┘                └─── mirror
         Gitee (主)                    GitHub (辅)
```

- `origin` → Gitee，主仓库，所有变更的权威来源
- `mirror` → GitHub，镜像仓库，由脚本单向/双向同步维护

## 二、同步算法

脚本 `scripts/sync.js` 的执行顺序：

### 阶段一：`alignLocalToPrimary()` —— 本地对齐 Gitee

比较本地 `HEAD` 与 `origin/main`：

| 关系 | 处理动作 |
|---|---|
| 一致 | 无操作 |
| 本地领先 | 本地提交 `push` 到 Gitee |
| Gitee 领先 | 本地 `merge --ff-only` 快进 |
| 分叉 | 以 Gitee 为 ours 执行 `merge -X ours`，再发布到 Gitee |

> 分叉时 Gitee 侧内容优先，本地与 Gitee 冲突的改动会被丢弃，
> 但本地独有的非冲突提交仍会被合并进去。

### 阶段二：`mirrorToPrimary()` —— GitHub 回灌 Gitee

比较 `origin/main` 与 `mirror/main`：

| 关系 | 处理动作 |
|---|---|
| 一致 | 无操作，本次同步结束 |
| Gitee 领先 | 快进推送到 GitHub |
| GitHub 领先且无分叉 | Gitee 快进到 GitHub（吸收 GitHub 的提交） |
| 两端分叉 | 以 Gitee 为 ours 执行 `merge -X ours`，结果同时推送到两端 |

### 关键设计：`merge -X ours`

合并时 Git 以 **当前 HEAD 所在侧** 为 `ours`。脚本在阶段一、二中均保证
**HEAD 站在 Gitee 一侧**再执行合并，因此 `-X ours` 始终代表「Gitee 内容优先」。

这实现了「以 Gitee 为主」的需求：GitHub 上的改动若与 Gitee 冲突，
保留 Gitee 的版本；不冲突的部分则被吸收。

## 三、冲突无法自动处理的情况

`-X ours` 只能解决**内容级冲突**。以下情况会导致合并失败并自动回滚：

- 同一文件被双方删除/修改（modify/delete 冲突）
- 二进制文件冲突
- 同一路径双方各自新增且内容不同

此时脚本会输出错误并执行 `git merge --abort`，**两端仓库状态保持不变**，
需人工介入。处理方式：

```bash
cd C:/Users/colorful/WorkBuddy/hengjian-knowledge-hub
git fetch origin mirror
git checkout main
git merge origin/main        # 在主仓库侧查看/解决冲突
# 手工解决后
git push origin main
git push mirror main
```

## 四、工作区未提交改动

脚本运行前检测到未提交改动时，会先 `git stash push --include-untracked`
暂存，同步完成后自动 `git stash pop` 恢复，避免合并操作被干扰。

## 五、日志

每次同步写入 `logs/sync-YYYY-MM-DD.log`，格式：

```
[2026-11-09T16:20:00.000Z] [INFO] ════ 恒剑知识库双向同步开始 ════
[2026-11-09T16:20:01.120Z] [INFO] 主仓库=Gitee (主)  镜像仓库=GitHub (辅)  分支=main
[2026-11-09T16:20:01.350Z] [INFO] 本地与Gitee (主)已一致 (a1b2c3d4)
[2026-11-09T16:20:01.600Z] [INFO] 两端已完全一致 (a1b2c3d4)
```

`logs/` 目录已加入 `.gitignore`，不会进入版本库。

## 六、常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| `推送 gitee 失败: Authentication failed` | 凭据过期 | 重新执行一次 `git push` 触发凭据管理器输入 |
| `两端合并失败，已回滚` | modify/delete 或二进制冲突 | 见上文第三节，人工介入 |
| `请求失败：The requested URL returned error: 403` | Gitee 私有仓库匿名访问被拒 | 使用带凭据的 HTTPS 地址（当前配置已正确） |
| 本地有未推送提交却未被同步 | 处于 `--dry-run` 模式 | 去掉 `--dry-run` 重新执行 |
