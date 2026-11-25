# PeerGrade V2.4 竞赛技术交付材料

**版本：** 2026-11-22 本机验收。材料面向计算机设计大赛技术评审，依据当前项目代码及合成验收，不代表正式公网部署或真人实验已经完成。校方当届提交格式与最终信息仍需确认。

## 报告与图示

| 文件 | 内容 |
| --- | --- |
| [PeerGrade 技术设计报告 Word](PeerGrade-技术设计报告-V2.4.docx) | 11 页可编辑报告，涵盖需求、架构、数据、算法、AI、工程验收及发布边界；ER 图单独横向排版 |
| [PeerGrade 技术设计报告 PDF](PeerGrade-技术设计报告-V2.4.pdf) | 与已核对的 Word 版相同的打印版本 |
| [系统架构图 SVG](figures/system-architecture.svg) / [PNG](figures/system-architecture.png) | 同源访问、服务模块、数据库、运维和可选模型接口 |
| [数据库 ER 图 SVG](figures/database-er-diagram.svg) / [PNG](figures/database-er-diagram.png) | 16 张实际 ORM 应用表、主键/外键/唯一约束及业务关系 |
| [互评流程图 SVG](figures/learning-workflow.svg) / [PNG](figures/learning-workflow.png) | 从教师任务到学生原文证据、反馈、采纳修订的过程 |
| [数据库数据字典](database-data-dictionary.md) / [结构 JSON](database-schema.json) | 从 ORM 元数据提取 16 张表、89 个字段和实际关联约束 |

## 实现说明与验证证据

- [匿名双评算法及原文证据锚定](algorithm-and-evidence.md)：实际代码、五人示例、复杂度与正确性分析，以及通过服务端校验的 Unicode 锚点示例。
- [AI 检测提示词与输出契约](ai-contract.md)：程序当前系统提示词、合成输入、输出和经过服务端重算的诊断位置；未调用外部模型。
- [自动化验收汇编](verification-evidence.md) / [结构化证据 JSON](verification-evidence.json)：SQLite 与 PostgreSQL 各 123 项、前端 26 项、并发、备份恢复及真实浏览器流程；保留实际接口类别时延和隔离验收边界。
- [源码校验清单](source-manifest.json)：实际源码及验收副本逐文件 SHA-256 对应（54 项），不含业务数据库或密钥；不是 Git 提交号。
- [项目介绍](project-introduction.md)：可用于展示首页、申报材料或后续答辩稿。
- [实验报告填写结构](experiment-report-template.md)：实施流程、人工量规、统计表和真实模型证据栏。目前未取得真人数据，故不是已完成的实测实验报告。

## 仍需完成的交付

- 3–5 分钟实际项目演示视频及正式答辩 PPT（继续制作）。
- 获得授权的真实 DeepSeek 接口验证、人工盲评和真实实验报告。
- 公网服务器、域名、Docker Compose 目标部署、远程 CI 和正式发布版本核验。

不得把本机合成测试或模拟响应填写成真实模型表现及教学实验结论；正式提交时根据学校实际通知调整格式和必要信息。
