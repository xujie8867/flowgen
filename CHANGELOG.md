# Changelog

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
