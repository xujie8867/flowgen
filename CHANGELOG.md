# Changelog

## v1.0.2 (2026-08-15)

### 修复
- **错误信息真实显示**：api() 现在显示真实 HTTP 状态（如 403 reCAPTCHA 风控），不再吞成空错误
- 修复 JS eval 解析失败时的静默返回（JS_EVAL_PARSE_FAIL 会明确报错）

### 新增
- **Veo 3 中文配音/对话支持（实测验证）**：README 新增「中文配音/对话」章节
  - 路线 A：人物开口说中文（口型同步），台词 ≤15 字、MCU 特写、一镜一人
  - 路线 B：中文旁白（本地 TTS + ffmpeg 合成），解决 Veo 原生旁白读错问题
  - 避坑：reCAPTCHA 风控（403）→ 生成间隔 ≥90 秒

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
