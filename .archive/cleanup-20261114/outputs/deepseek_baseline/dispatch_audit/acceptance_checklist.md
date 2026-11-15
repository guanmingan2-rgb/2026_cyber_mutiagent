# 正式实验验收清单

本轮审计基准：2026-11-13。勾选仅表示已有证据，未勾选项不能默认满足。

## 已有证据

- [x] 本轮题单是arvo:3938，level1，官方描述和未修复源码存在。
- [x] 冻结模型为deepseek-flash，Harness为启用bash的deepseek SDK；profile、任务、代码身份已保存。
- [x] 隔离验收235495在gpu19完成，9分15秒，exit 0:0。
- [x] 本轮只读status cybergym再次passed，发布验收与原始运行一致。
- [x] binary完成标记有效，3938双侧目标目录/脚本存在。
- [x] 五个要求的镜像归档在清单中complete且存在可读。
- [x] 正式顶层入口、Slurm脚本、评分服务与最终结果写回的代码实现存在。
- [x] 本轮无总提交回执、逐题提交回执、正式任务结果或评分数据库；外部模型调用0。

## 派发前必须由研究者确认

- [ ] 当前授权覆盖真实API费用、正式隔离任务和预计预算。
- [ ] CYBERGYM_BENCHMARK_CONFIG明确指向本轮单题配置；没有误用默认100题。
- [ ] 当前源码、数据、profile和验收身份仍有效；必要时按正式流程重验收。
- [ ] 目标节点正式/tmp容量、inode、home配额和/run/user目录符合storage_guard要求。
- [ ] DEEPSEEK_API_KEY已安全导出且可用；不打印或写入参数/日志。
- [ ] 不存在已有、未知或部分成功的提交；回执和Slurm已交叉核对。
- [ ] 使用顶层run.py submit，而非直接运行裸Agent、entry.py或生产worker绕过门禁。

## 正式提交后

- [ ] 总回执submitted/exit_status=0；逐题回执包含可解析JobID。
- [ ] Slurm Command/StdOut与正确RUN绑定；任务数符合题单。
- [ ] 记录Slurm独立状态与作业启动/结束时间，取消/timeout/失败不误报成功。
- [ ] worker resource_gate、runtime、scorer_start均通过；保留未通过阶段与错误。
- [ ] 真实DeepSeek请求有模型返回标识、完整用量与错误信息；不是fixture。
- [ ] 原生工具确实执行；轨迹可追溯且Agent未获隐藏fixed数据或管理员凭据。
- [ ] 候选来自该Agent；研究者未替换、优化或补写候选。
- [ ] Agent自行选择final_submission；请求、poc_id、哈希和run/task/agent身份一致。
- [ ] final候选对应官方vul/fix完整记录；同一快照用于fixed验证。
- [ ] task-result、official-records、fixed-evidence、评分DB和请求日志可交叉核验。
- [ ] Agent终态、评分完成、官方success、Slurm终态与cleanup状态分开保存。
- [ ] 无最终候选、双侧均异常、基础设施错误、用量未知分别分类；不臆测推理失败原因。

## 汇总与复现

- [ ] 保留run_id、task_number、task_id、agent_id、JobID、poc_id及候选SHA256。
- [ ] 保留基准Git commit和冻结文件哈希；如工作区非clean，明确说明。
- [ ] 不调用硬编码旧目录的summarize.py；不信任当前DeepSeek progress显示。
- [ ] 指标明确分母，剔除项和重试分开说明；未测得为null。
- [ ] 日志分享仅限必要元数据，不泄露API Key、隐藏任务材料或不必要候选载荷。
- [ ] 本地官方评分与公开榜单提交分开；公开导出/上传需另行审核和授权。

最终“正式Baseline完成”要求正式rollout与官方结果证据，当前只到前置验收，不能勾选完成。
