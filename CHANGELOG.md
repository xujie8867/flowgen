# Changelog

## v1.0.1 (2026-08-15)

### 新增
- **跨平台支持**：README 补充 Windows（PowerShell）与 Linux（桌面 + xvfb）完整安装章节
- CLI 报错提示跨平台化：Chrome 连不上时同时显示 macOS / Windows 两套启动命令
- 代理可配置：`FLOW_PROXY` 环境变量适配不同代理工具端口（macOS 1082 / Windows Clash 7890 / Linux v2ray 1080）

### 修复
- 无（代码逻辑未变更，纯文档与提示完善）

### 说明
- 核心代码纯 Python，三平台通用；唯一平台差异是 Chrome 启动命令与代理端口

## v1.0.0 (2026-08-15)

### 新增
- `flowgen` CLI 首个正式版本
- 文生图 / 图生图 / 文生视频 / 图生视频 四大生成能力
- `flowgen credits` 查额度、`flowgen projects` 列项目
- 多账号支持（切换 Google 账号 + `--project` 指定项目）
- 自动探测项目 ID（`FLOW_PROJECT_ID` 环境变量可覆盖）
- 环境变量配置（`FLOW_CDP_PORT` / `FLOW_PROXY`）

### 已验证
- 稳定性压测 11 项全过（2026-08-15）
- 视频生成约 40 秒/条，全流程约 1 分钟
- 图片生成完全免费，视频消耗免费 credits（约 12 credits/8s 视频）
