"""多 Agent 编排图组装。

拓扑：
    START → load_context ─┬→ sentiment_agent ─┐
                          └→ orchestrator ────┴→ join（汇聚，等两分支都完成）
                                                    │ 条件路由 pick_agent
                              ┌── recipe_agent ⇄ recipe_tools（ReAct 循环）
                              ├── dev_agent ⇄ dev_tools（ReAct 循环）
                              └── chat_agent
                                                    ↓
                                                   END
"""
from langgraph.graph import END, START, StateGraph

from app.graph import nodes
from app.graph.state import GraphState


def build_graph():
    graph = StateGraph(GraphState)

    graph.add_node("load_context", nodes.load_context)
    graph.add_node("sentiment_agent", nodes.sentiment_node)
    graph.add_node("orchestrator", nodes.orchestrator_node)
    graph.add_node("join", nodes.join_node)
    graph.add_node("recipe_agent", nodes.recipe_agent)
    graph.add_node("recipe_tools", nodes.recipe_tools_node)
    graph.add_node("dev_agent", nodes.dev_agent)
    graph.add_node("dev_tools", nodes.dev_tools_node)
    graph.add_node("chat_agent", nodes.chat_agent)

    graph.add_edge(START, "load_context")
    # 情感分析与编排分类并行：互不依赖，各写各的 state key
    graph.add_edge("load_context", "sentiment_agent")
    graph.add_edge("load_context", "orchestrator")
    graph.add_edge("sentiment_agent", "join")
    graph.add_edge("orchestrator", "join")

    graph.add_conditional_edges("join", nodes.pick_agent)
    graph.add_conditional_edges("recipe_agent", nodes.recipe_should_continue)
    graph.add_edge("recipe_tools", "recipe_agent")
    graph.add_conditional_edges("dev_agent", nodes.dev_should_continue)
    graph.add_edge("dev_tools", "dev_agent")
    graph.add_edge("chat_agent", END)

    return graph.compile()


chat_graph = build_graph()
