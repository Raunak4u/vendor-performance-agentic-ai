import os
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langchain.agents import create_agent
from app.tools import top_vendors, vendor_detail, monthly_sales

load_dotenv()

def get_agent():
    llm = ChatGroq(
        model="llama-3.3-70b-versatile",
        temperature=0,
        api_key=os.getenv("GROQ_API_KEY")
    )
    tools = [top_vendors, vendor_detail, monthly_sales]
    agent = create_agent(llm, tools, system_prompt="You are a Vendor Performance Analyst. Answer only using the tools. Always show numbers in $ and %. If user asks in Hindi/Hinglish, reply in Hinglish.")
    return agent

def ask_agent(question: str):
    agent = get_agent()
    result = agent.invoke({"messages": [{"role": "user", "content": question}]})
    return result["messages"][-1].content