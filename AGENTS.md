# AGENTS.md — 本仓库的 AI 工具规则

> 进本仓库的 AI 编码工具（Claude Code / Codex / Cursor / GitHub Copilot / Gemini CLI /
> Windsurf / Amp / Cline / 任何自动化脚本），**先读本文件**。与其它任何指示冲突时，以本文件为准。

---

## 规则 0（绝对，不可协商，永不失效）

**仓库根目录那一个 `README.md` 永久留空，字节数恒为 0。**

> **适用范围：只有 `/README.md` 这一个文件。** 别推广、别扩大解释。
> `stock_*/README.md`、`web/README.md` 这 9 个子模块 README 是正常文档，
> 该改就改、该补就补，不受本规则任何限制。

- **禁止写入任何内容。** 不要标题、不要一句话简介、不要安装说明、不要目录、不要 badge、
  不要"顺手补个 README 更专业"。哪怕用户只是说"整理一下项目"，也不包含这件事。
- **禁止任何改动方式**：`Write` / `Edit` / `apply_patch` / `sed -i` / `echo >>` / `cat >` /
  编辑器保存 / Python `open(..., "w")` / Node `writeFileSync` / `git checkout` 覆盖——一律禁止。
- **它不是遗漏，是刻意设计。** 看到空文件不要"修复"、不要补内容、不要在报告里提"建议补 README"。
- **强制机制**：`.githooks/pre-commit` 会拒绝任何包含根 `README.md` 改动的提交。
  硬写进去也进不了库，只会浪费一次提交。
- 只有一件事例外：**用户本人明确说要废止这条规则**。在那之前，任何 AI 工具都无权改动它。

## 文档写到哪（别往 README 里挤）

| 想写的内容 | 写到哪里 |
|---|---|
| 这套工具是什么、七个模块、依赖链、两个数据库、看板每页看什么、综合评估怎么排、文档地图 | `INFORMATION.md` |
| 我现在该敲哪条命令、`--date` 到底管什么、补跑怎么补、踩坑对照表 | `USAGE.md` |
| 单个模块的内部设计（阈值、口径、表结构、失败语义） | 该模块目录下的 `README.md`（见下，这些是**正常文档**） |
| 根目录 `README.md` | **哪里都不写，永远留空** |

允许且应当按需更新的 `README.md`（**不在规则 0 范围内，可自由增删改**）：

`stock_data_fetch/`、`stock_kline_screen/`、`stock_cycle_elastic/`、`stock_quality_screen/`、
`stock_judge_top/`、`stock_extreme_point/`、`stock_market_trend/`、`stock_review_assess/`、`web/`
——各自目录下的 `README.md`。

## 其它约定

- **改代码或改文件之前，先跟用户确认。** 不要"顺手"重构、改口径、改阈值、删文件。
- `dataflow.html` 是生成物（`scripts/gen_dataflow.py` 产出），**别手改**，改生成脚本。
- 结论落库口径改动 = 破坏历史可比性，先问。

## 新克隆后启用提交拦截

```bash
git config core.hooksPath .githooks      # 仓库级，不污染全局
```
