# 设计文档索引

该目录是 `public-source-collector` 的开发契约。新开发者或 AI Agent 不需要先通读源码，按下列顺序阅读即可开始工作。

| 文档 | 内容 | 何时阅读 |
|---|---|---|
| [../ARCHITECTURE.md](../ARCHITECTURE.md) | 目标、锁定决策、模块与运行流程 | 必读，先读 |
| [IMPLEMENTATION_GUIDE.md](IMPLEMENTATION_GUIDE.md) | 技术栈、目录、开发阶段、测试和完成定义 | 开始编码前必读 |
| [DATA_AND_API_CONTRACTS.md](DATA_AND_API_CONTRACTS.md) | 数据模型、状态、事务和 HTTP 契约 | 改数据库或接口前必读 |
| [ADMIN_UI.md](ADMIN_UI.md) | 页面、交互、状态、权限和前端验收 | 开发管理站点前必读 |
| [NEWS_DAILY_CONSOLE_DESIGN.md](NEWS_DAILY_CONSOLE_DESIGN.md) | 资讯日报控制台：产品闭环、版本、草稿上传、接口和实施阶段 | 开发新的日报管理站点前必读 |

## 文档规则

1. 目标设计与当前实现必须明确区分。
2. 状态名、字段名和接口路径只在契约文档中定义一次，其他文档通过链接引用。
3. 改变锁定决策时，先更新文档，再改代码和测试。
4. 实现新增状态或错误码时，同步更新数据/API 契约。
5. 页面不得创造后端不存在的状态；后端不得返回页面没有处理的终态。
6. 完成一个实施阶段后，更新实施指南中的状态表和 README 项目状态。

## Agent 快速指令

接手任务时先执行：

```bash
cd /Users/wangkp/Documents/wechat-paper-collect/public-source-collector
python -m unittest discover -s tests -v
```

然后确认任务属于实施指南中的哪个阶段，只修改该阶段涉及的模块和契约。测试不得访问真实微信；真实协议验证必须是人工显式操作。
