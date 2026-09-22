# 项目规则

## Git 提交与推送

- 每次执行 `git commit` 后必须立即推送到远程,不得只提交不推送。
- Windows 侧 git 推送 GitHub 会报 `schannel: SSL/TLS connection failed`,推送必须通过 WSL 内的 git 执行:
  `wsl -d Ubuntu-22.04 --cd /home/ahang/ai/demo/zpc/paper-service -- git push`
- 提交信息使用简短中文,与仓库现有风格一致(如"更新paper-navigator首轮默认网页搜索")。
- 远程仓库: `origin` → `https://github.com/ahang1598/paper-service.git`
