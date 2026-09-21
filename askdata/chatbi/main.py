# main.py
"""ChatBI 意图流水线本地测试入口。支持：

- 项目根目录：python -m chatbi.main  /  python chatbi/main.py
- chatbi 目录：python main.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

# 允许在 chatbi/ 下直接 python main.py
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from chatbi.chat_workflow import ChatBIWorkflow  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("chatbi.main")


class MockLLMClient:
    """本地 Mock，无需真实大模型即可跑通整套链路。"""

    async def chat(self, system: str, user: str) -> str:
        _ = system
        if "触发LLM失败" in user:
            raise RuntimeError("模拟 LLM 调用失败，验证规则降级")

        if "成本GAP以及其他相关的原子和派生指标" in user:
            return """
{
  "意图来源": "LLM结构化抽取",
  "意图类型": "数据查询",
  "指标": ["成本GAP"],
  "币种": "人民币(CNY)",
  "分析维度": [],
  "筛选条件": {},
  "计算指令": null,
  "是否查询关联指标": true,
  "原始问句": "所有项目的成本GAP以及其他相关的原子和派生指标，人民币"
}
"""
        if "成本GAP是什么口径" in user:
            return """
{
  "意图来源": "LLM结构化抽取",
  "意图类型": "指标口径咨询",
  "指标": ["成本GAP"],
  "币种": "",
  "分析维度": [],
  "筛选条件": {},
  "计算指令": null,
  "是否查询关联指标": false,
  "原始问句": "成本GAP是什么口径？"
}
"""
        if "按项目看前10个成本GAP，美元" in user:
            return """
{
  "意图来源": "LLM结构化抽取",
  "意图类型": "数据查询",
  "指标": ["成本GAP"],
  "币种": "美元(USD)",
  "分析维度": ["项目"],
  "筛选条件": {},
  "计算指令": "TopN",
  "是否查询关联指标": false,
  "原始问句": "按项目看前10个成本GAP，美元"
}
"""
        if "成本GAP和PJ成本金额" in user:
            return """
{
  "意图来源": "LLM结构化抽取",
  "意图类型": "数据查询",
  "指标": ["成本GAP", "PJ成本金额"],
  "币种": "人民币(CNY)",
  "分析维度": [],
  "筛选条件": {},
  "计算指令": null,
  "是否查询关联指标": false,
  "原始问句": "成本GAP和PJ成本金额"
}
"""
        # 从 user_payload 中尽量还原原始问句
        raw = user
        if "用户问句：" in user:
            raw = user.split("用户问句：", 1)[1].split("\n", 1)[0].strip()

        return json.dumps(
            {
                "意图来源": "LLM结构化抽取",
                "意图类型": "数据查询",
                "指标": [],
                "币种": "",
                "分析维度": [],
                "筛选条件": {},
                "计算指令": None,
                "是否查询关联指标": False,
                "原始问句": raw,
            },
            ensure_ascii=False,
        )


async def main() -> None:
    llm_client = MockLLMClient()
    workflow = ChatBIWorkflow(llm_client=llm_client)
    session_id = "test_session_001"

    test_queries = [
        "所有项目的成本GAP以及其他相关的原子和派生指标，人民币",
        "成本GAP是什么口径？",
        "按项目看前10个成本GAP，美元",
        "看项目的毛利",
        "成本GAP和PJ成本金额",
        "触发LLM失败：查一下成本GAP，人民币",
    ]

    print("===== ChatBI 意图识别本地测试 =====")
    for q in test_queries:
        print(f"\n【用户提问】：{q}")
        result = await workflow.run(user_query=q, session_id=session_id)
        print(f"【返回结果】：{result}")

    if not sys.stdin.isatty() or "--batch" in sys.argv:
        logger.info("非交互模式结束（stdin 非 TTY 或传入 --batch）")
        return

    print("\n===== 交互式测试模式，输入 exit 退出 =====")
    while True:
        user_input = input("\n请输入查询语句：")
        if user_input.strip().lower() == "exit":
            break
        res = await workflow.run(user_query=user_input, session_id=session_id)
        print("输出结果：", res)


if __name__ == "__main__":
    asyncio.run(main())
