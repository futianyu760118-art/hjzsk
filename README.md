# 恒剑知识库 (hengjian-knowledge-hub)

宁波恒剑光电科技有限公司 · 统一知识库

本仓库为 **Gitee 为主仓库**，通过自动化脚本与 **GitHub** 保持双向同步。

| 角色 | 平台 | 地址 | 可见性 |
|---|---|---|---|
| **主仓库** | Gitee | `https://gitee.com/ningbohengjianguangdian/hengjian-knowledge-hub.git` | 私有 |
| **镜像仓库** | GitHub | `https://github.com/futianyu760118-art/hjzsk.git` | 公开 |

## 同步规则

以 **Gitee 为准**（single source of truth）：

1. 本地未推送的提交 → **优先发布到 Gitee**
2. Gitee 领先 → 快进推送到 GitHub
3. GitHub 独有提交 → 回灌合并进 Gitee
4. 两端分叉 → 执行合并，**冲突一律保留 Gitee 内容**（`git merge -X ours`）

## 目录结构

```
.
├── README.md          本文件
├── docs/
│   └── sync-guide.md   同步机制与故障排查
├── scripts/
│   ├── sync.js         双向同步引擎（核心）
│   └── sync.sh         Linux / Git Bash 包装脚本
└── logs/               同步日志（本地，不入库）
```

## 常用命令

```bash
# 完整同步（拉取两端 + 合并 + 双推）
node scripts/sync.js

# 只查看两端差异状态，不做任何修改
node scripts/sync.js --status

# 试演：走一遍流程但不真正推送
node scripts/sync.js --dry-run
```

## 自动化

同步由 WorkBuddy 自动化定时触发，工作目录为本仓库，
执行 `node scripts/sync.js`，日志写入 `logs/sync-YYYY-MM-DD.log`。
