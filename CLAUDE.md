# CLAUDE.md

@AGENTS.md

## 硬规则复述（防止只读片段）

**仓库根目录 `README.md` 永久留空，恒为 0 字节。禁止写入、禁止改动、禁止"帮你补个 README"。**

> 本规则**只约束这一个文件**（`/README.md`）。`stock_*/README.md`、`web/README.md` 是正常文档，
> 该改就改，不受限制，别把规则外溢过去。

- 不要在根 `README.md` 里创建标题、简介、安装说明、目录、badge，任何内容都不行。
- 它是刻意留空，不是遗漏，不要"修复"。
- 文档去向：`INFORMATION.md`（项目是什么）、`USAGE.md`（按情景查命令）、
  各 `stock_*/README.md` 与 `web/README.md`（模块内部设计，这些可正常更新）。
- 提交时 `.githooks/pre-commit` 会拒绝任何含根 `README.md` 改动的 commit。
