# Codex 多分支协作流程

本项目推荐使用 `git worktree` 让多个 Codex 对话并行工作。

核心规则：

```text
一个任务 = 一个分支 = 一个独立 worktree 目录 = 一个 Codex 对话
```

不要让多个 Codex 对话共用同一个目录切换不同分支。因为同一个工作目录执行 `git checkout` 会影响所有正在看这个目录的对话。

## 主目录

当前主目录：

```text
E:\英杰大战环境数据库
```

主目录保留 `main` 分支，用来同步远端、创建 worktree、处理紧急小修。日常功能开发尽量不要直接在主目录改。

## 创建新任务目录

在 PowerShell 中执行：

```powershell
git -C "E:\英杰大战环境数据库" checkout main
git -C "E:\英杰大战环境数据库" pull
git -C "E:\英杰大战环境数据库" worktree add "E:\ekisuleaderboard-任务名" -b feature/任务名 main
```

示例：

```powershell
git -C "E:\英杰大战环境数据库" worktree add "E:\ekisuleaderboard-mobile" -b feature/mobile-leaderboard main
```

然后在 Codex 中打开新目录：

```text
E:\ekisuleaderboard-mobile
```

这个 Codex 对话只处理 `feature/mobile-leaderboard` 分支。

## 每个 Codex 对话开始前

先确认目录和分支：

```powershell
git status --short --branch
git remote -v
```

期望看到：

```text
## feature/任务名
```

如果不在目标分支，先停下来，不要直接改文件。

## 给 Codex 的推荐开场指令

可以在新对话里这样说：

```text
请先阅读 docs/codex-branch-workflow.md，并确认当前目录和分支。
这个任务只在当前 feature 分支处理，不要切换 main。
不要修改与任务无关的文件。
```

如果任务会影响部署、GitHub Actions 或数据安全边界，也建议补一句：

```text
改动后请检查 .gitignore、安全路径和相关测试。
```

## 开发过程约定

- 一个对话不要切到其它任务分支。
- 不要两个对话同时改同一个分支。
- 尽量避免两个任务同时改同一个文件；如果必须改，先合并其中一个 PR，再让另一个分支从 `main` 更新。
- 不要提交 `data/`、`.tmp/`、`.venv/`、`build/`、`dist/`、`.env`、本地 token、cookies、浏览器 profile。
- exe 包体仍作为发布产物，不进 Git。

## 提交和推送

在任务 worktree 目录中执行：

```powershell
git status
git add .
git commit -m "简短说明本次任务"
git push -u origin feature/任务名
```

然后在 GitHub 开 Pull Request 合并到 `main`。

当前自动化规则：

```text
push 到 feature/*：适合跑测试和开 PR，不部署 VPS。
push 到 main：测试通过后自动部署 VPS。
```

## 更新分支

如果 `main` 已经有新改动，功能分支需要跟上：

```powershell
git fetch origin
git merge origin/main
```

遇到冲突时，不要随手覆盖。先看冲突文件，保留两边有效改动。

## 删除已完成 worktree

PR 合并且不再需要本地目录后：

```powershell
git -C "E:\英杰大战环境数据库" worktree remove "E:\ekisuleaderboard-任务名"
git -C "E:\英杰大战环境数据库" branch -d feature/任务名
```

如果远端分支也确认不需要：

```powershell
git -C "E:\英杰大战环境数据库" push origin --delete feature/任务名
```

## 常用检查

列出所有 worktree：

```powershell
git -C "E:\英杰大战环境数据库" worktree list
```

查看当前分支：

```powershell
git branch --show-current
```

查看当前改动：

```powershell
git status --short
```

查看最近提交：

```powershell
git log --oneline --decorate -5
```
