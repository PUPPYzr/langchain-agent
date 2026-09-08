# LangChain Weather Agent

这是一个使用 LangChain 构建的命令行天气 Agent。用户输入地点后，Agent 会调用 Open-Meteo 查询地点和当前天气，再由模型生成中文摘要。

## 项目结构

```text
langchain-agent/
├── weather_agent/
│   ├── agent.py             # 模型和 Agent 组装
│   ├── config.py            # 环境变量配置
│   ├── tools.py             # LangChain 天气工具
│   └── weather_service.py   # Open-Meteo API 客户端
├── .env.example
├── main.py                  # 命令行入口
└── requirements.txt
```

## 创建环境

Python 需要 3.10 或更高版本。

```powershell
cd D:\software\project\langchain-agent

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

也可以使用 Conda 创建环境，然后执行同一条依赖安装命令。

## 配置模型

```powershell
Copy-Item .env.example .env
```

编辑 `.env`：

```dotenv
OPENAI_API_KEY=你的密钥
OPENAI_MODEL=你的模型ID
```

如果使用兼容 OpenAI API 的服务，再配置：

```dotenv
OPENAI_BASE_URL=https://你的服务地址/v1
```

天气数据由 Open-Meteo 提供，不需要天气 API Key。

## 运行

```powershell
python main.py 北京
```

默认模式直接调用 Open-Meteo 并在本地格式化结果，不需要等待大模型生成摘要，适合常规天气查询。

如果需要演示完整的 LangChain Agent 流程（模型决定调用工具，再由模型总结结果），使用：

```powershell
python main.py 北京 --agent
```

或者进入交互输入：

```powershell
python main.py
```

## 自定义方向

- 在 `tools.py` 增加未来几天天气、空气质量或天气预警工具。
- 在 `SYSTEM_PROMPT` 中增加穿衣、出行等业务规则。
- 将 `main.py` 替换为 FastAPI 接口、聊天机器人或网页后端。
- 如果地点重名，可调整地点选择策略，让用户确认候选地点。

生产环境还应增加请求重试、日志、限流、缓存和监控。
