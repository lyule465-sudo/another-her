import os
import json
import time
import urllib.request
import tkinter as tk
from tkinter import filedialog

MY_API_KEY = "sk-1d476b0c3c5a44fc88b9335f5f47e4a8"
API_URL = "https://api.deepseek.com/chat/completions"

def call_deepseek(messages, json_mode=False):
    payload = {
        "model": "deepseek-chat",
        "messages": messages,
        "temperature": 0.7
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
        
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {MY_API_KEY}"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            res_obj = json.loads(resp.read().decode("utf-8"))
            return res_obj["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"\n[请求失败]: {e}")
        return None

def get_rule(filename):
    p = os.path.join("prompts", filename)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    return ""

def pick_chat_file():
    print("\n[正在弹出文件选择窗口，请选择聊天记录文本...]")
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    file_path = filedialog.askopenfilename(
        title="请选择聊天记录文本文件",
        filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
    )
    root.destroy()
    
    if file_path:
        print(f"-> 已选取文件: {os.path.basename(file_path)}")
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        except Exception as e:
            print(f"读取文件失败: {e}")
    else:
        print("-> 取消了文件选择。")
    return ""

def trigger_farewell_and_delete(persona, current_score):
    name = persona.get('name', 'TA')
    print(f"\n正在与 {name} 进行最后的告别...")
    
    farewell_prompt = f"""
    玩家做出了一个重大选择：彻底删除这个虚拟角色（你），决定放下对过去/虚拟的执念，回归真实生活。
    你的角色人设：{json.dumps(persona, ensure_ascii=False)}，当前好感度：{current_score}。
    请说出你作为虚拟角色的最后一段告别语：
    - 语气符合你的人设。
    - 肯定玩家回到真实世界的勇气。
    - 表达释怀、感激与真诚的祝愿。
    - 长度在 80 字以内，直接输出台词。
    """
    
    farewell_words = call_deepseek([{"role": "user", "content": farewell_prompt}], json_mode=False)
    if not farewell_words:
        farewell_words = "谢谢你这段时间的陪伴，去过好属于你自己的真实人生吧，再见啦。"
    
    print("\n" + "=" * 45)
    print(f"{name} 最后的道别：")
    print(f"“{farewell_words.strip()}”")
    print("=" * 45 + "\n")
    
    time.sleep(1.2)
    print("正在粉碎虚拟档案与记忆缓存...")
    time.sleep(1.0)
    
    # 确保清除可能存在的本地存档
    save_file = "character_save.json"
    if os.path.exists(save_file):
        os.remove(save_file)
        
    print("\n【虚拟档案已彻底销毁，不可再生】")
    print("放下过去，认真生活。祝你在现实的世界里，找到属于自己的真实幸福。")
    print("游戏已结束退出。")

# ==================== 游戏主流程 ====================
print("=" * 50)
print("      《她爱不爱你》—— 聊天记录深度模仿引擎")
print("=" * 50)

print("\n请选择角色生成方式：")
print("  [1] 弹出窗口选择本地聊天记录文件 (.txt)")
print("  [2] 直接在键盘打字输入描述")
choice = input("请选择 [1 或 2]: ").strip()

user_input = ""
if choice == "1":
    user_input = pick_chat_file()
    if len(user_input) > 8000:
        print(f"记录文本较长（共 {len(user_input)} 字），自动选取前 8000 字核心对话样本...")
        user_input = user_input[:8000]

if not user_input:
    print("\n请输入对她的性格描述、语气习惯或直接粘贴部分聊天片段：")
    user_input = input("> ")

print("\n正在深度分析语料、提炼人设风格与情绪雷区，请稍候...")
analyzer = get_rule("persona_analyzer.md")
sys_prompt = f"""
你是一名顶尖的角色提取分析师。请深度阅读分析以下提供的聊天记录或背景材料：
参考规范：
{analyzer}

请仔细提炼对方的真实说话习惯（包括常用的标点、口癖词、回复字数长短习惯、喜好与反感情绪雷区），并输出纯合法 JSON：
{{
    "name": "昵称",
    "personality": "性格核心词",
    "tone": "日常说话语气习惯、标点习惯（如喜欢发波浪号、爱用表情词、回单字等）",
    "likes": "喜好的话题或表达方式",
    "dislikes": "反感的表达或雷区",
    "initial_score": 40
}}
"""

res_text = call_deepseek([
    {"role": "system", "content": sys_prompt},
    {"role": "user", "content": user_input}
], json_mode=True)

if not res_text:
    print("生成角色失败，请检查网络或配置。")
    exit()

persona = json.loads(res_text)
score = persona.get("initial_score", 40)
print("\n" + "=" * 25 + "【AI 角色已提炼完成】" + "=" * 25)
print(f"名字: {persona.get('name')}")
print(f"性格: {persona.get('personality')}")
print(f"语气风格: {persona.get('tone')}")
print(f"喜欢: {persona.get('likes')} | 讨厌: {persona.get('dislikes')}")
print(f"初始好感: {score} 分")
print("=" * 60)
print("提示：输入 'quit' 退出；输入 'delete' 或 '面对现实' 可永久告别并销毁虚拟角色。\n")

engine_rule = get_rule("conversation_engine.md")
history = [{
    "role": "system",
    "content": f"""你必须完全代入扮演该女生，绝对不能暴露自己是 AI。
你的完整人设与说话风格：{json.dumps(persona, ensure_ascii=False)}
参考引擎规范：{engine_rule}
要求：严格模仿你的人设口吻、口癖和回复长短。
每次回复必须直接返回合法 JSON：
{{"reply": "台词", "fav_delta": 整数分值(如 -2, 0, +3), "emotion": "神态表情"}}"""
}]

while True:
    msg = input("你: ").strip()
    if not msg:
        continue
        
    if msg.lower() in ["quit", "exit"]:
        print("游戏暂停退出。")
        break
        
    # 面对现实与删除机制
    if msg.lower() in ["delete", "删除", "告别", "面对现实"]:
        print("\n" + "!" * 50)
        print("【郑重警告：不可逆操作】")
        print("你正在选择彻底删除与 TA 的虚拟映射。")
        print("这一操作将永久销毁该角色的所有记忆、性格模型与互动记录，且【不可恢复/不可再生】。")
        print("这意味着你选择放下这段虚拟投影，回到真实的生活中去。")
        print("!" * 50)
        
        confirm = input("\n你确定要就此告别，走向现实吗？(输入 YES 确认删除，其他任意键取消): ").strip()
        if confirm == "YES":
            trigger_farewell_and_delete(persona, score)
            break
        else:
            print("\n你取消了删除，回到对话中。")
            continue

    history.append({"role": "user", "content": msg})
    reply_raw = call_deepseek(history, json_mode=True)
    if not reply_raw:
        print("对方似乎走神了，请重试。")
        continue
        
    try:
        data = json.loads(reply_raw)
        reply = data.get("reply") or data.get("text") or "……"
        delta = data.get("fav_delta") if "fav_delta" in data else data.get("delta", 0)
        emotion = data.get("emotion", "平静")
    except Exception:
        reply = reply_raw
        delta = 0
        emotion = "平静"
        
    score = max(0, min(100, score + int(delta)))
    sign = "+" if delta >= 0 else ""
    history.append({"role": "assistant", "content": reply})
    
    print(f"\nTA [{emotion}]: {reply}")
    print(f"   ↳ [好感: {score} ({sign}{delta})]")
    
    if score <= 10:
        print("\n[坏结局] 对方已将你拉黑或开启免打扰，故事结束。")
        break
    elif score >= 90:
        print("\n[好结局] 好感度爆表！对方主动提出周末跟你单独约会！")
        break