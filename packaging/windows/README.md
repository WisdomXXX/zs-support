# Windows 延后交付

按照开发计划 M5，Ubuntu 全机型实机验收完成后才开始 GUI/exe。
当前没有 Windows 可执行文件，不能将传输 Protocol 视为 Windows 已适配。

GUI 应使用 `Devices`、`Recording`、`Collection` 服务与 `TaskControl` 进度/取消接口，
在后台线程运行任务；实现 Paramiko/SFTP 后端、Windows 文件锁与 XDG 替代路径。
验收要求包括无 Python/SSH/rsync 预装的环境、续传、取消、会话恢复及 exe 安装卸载。
