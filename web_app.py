import os
import re
import json
import uuid
import time
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# ==================== 配置与存储目录 ====================
MY_API_KEY = "sk-1d476b0c3c5a44fc88b9335f5f47e4a8"
API_URL = "https://api.deepseek.com/chat/completions"
PORT = int(os.environ.get("PORT", 8080))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data", "sessions")
os.makedirs(DATA_DIR, exist_ok=True)
os.chdir(BASE_DIR)

sessions_lock = threading.Lock()
sessions = {}

# ==================== 磁盘持久化与记忆机制 ====================
def save_session_to_disk(sid):
    """持久化玩家档案到本地硬盘"""
    file_path = os.path.join(DATA_DIR, f"{sid}.json")
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(sessions[sid], f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[存档写入失败 {sid}]: {e}", flush=True)

def load_session_from_disk(sid):
    file_path = os.path.join(DATA_DIR, f"{sid}.json")
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None

def get_or_create_session(session_id):
    with sessions_lock:
        if session_id and session_id in sessions:
            return session_id, sessions[session_id]
        
        if session_id:
            loaded = load_session_from_disk(session_id)
            if loaded:
                sessions[session_id] = loaded
                return session_id, loaded
        
        new_id = str(uuid.uuid4())
        sessions[new_id] = {
            "persona": {},
            "score": 40,
            "stage": "初识试探",
            "key_memories": [],       # 长期压缩记忆池
            "history": [],            # 对话滑窗记录
            "original_context": "",   # 玩家开局提供的最初聊天记录
            "deduction_logs": [],     # 好感度下降瞬间与原因
            "is_over": False
        }
        save_session_to_disk(new_id)
        return new_id, sessions[new_id]

def extract_json_payload(raw_text: str) -> dict:
    """容错提取模型返回的 JSON"""
    if not raw_text:
        return {}
    text = raw_text.strip()
    if "```" in text:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if match:
            text = match.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end+1]
    try:
        return json.loads(text)
    except Exception:
        return {}

def call_deepseek(messages, json_mode=False, timeout=45):
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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            res_obj = json.loads(resp.read().decode("utf-8"))
            return res_obj["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[API 通信异常]: {e}", flush=True)
        return None

def get_prompt_file(filename):
    p = os.path.join(BASE_DIR, "prompts", filename)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    return ""

def compress_memories_if_needed(user_game):
    """当对话超过 14 条时，将最早 6 条提取为不可磨灭的长期记忆"""
    if len(user_game.get("history", [])) < 14:
        return

    old_slice = user_game["history"][:6]
    user_game["history"] = user_game["history"][6:]

    extract_prompt = f"""分析以下玩家与角色的早期对话，提取 1~2 条属于两人之间【核心事件、承诺、秘密或好恶事实】：
对话：{json.dumps(old_slice, ensure_ascii=False)}
输出纯 JSON：{{"facts": ["事实1", "事实2"]}}"""

    raw = call_deepseek([{"role": "user", "content": extract_prompt}], json_mode=True)
    res = extract_json_payload(raw)
    facts = res.get("facts", [])
    if facts and isinstance(facts, list):
        user_game.setdefault("key_memories", []).extend(facts)
        if len(user_game["key_memories"]) > 15:
            user_game["key_memories"] = user_game["key_memories"][-15:]

# ==================== 原生请求处理器 ====================
class MultiUserGameHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        clean_path = self.path.split("?")[0]
        if clean_path in ["/", "/index.html"]:
            file_path = os.path.join(BASE_DIR, "templates", "index.html")
            if os.path.exists(file_path):
                with open(file_path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            else:
                self.send_error(404, "templates/index.html not found")
                return
        self.send_error(404, "Not Found")

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"
        try:
            req_data = json.loads(body) if body else {}
        except Exception:
            req_data = {}

        raw_sid = req_data.get("session_id")
        session_id, user_game = get_or_create_session(raw_sid)

        # 1. 读取存档
        if self.path == "/api/load_session":
            has_role = bool(user_game.get("persona") and user_game["persona"].get("name"))
            self._send_json({
                "has_role": has_role,
                "persona": user_game.get("persona", {}),
                "score": user_game.get("score", 40),
                "stage": user_game.get("stage", "初识试探"),
                "history": user_game.get("history", []),
                "session_id": session_id,
                "is_over": user_game.get("is_over", False)
            })
            return

        # 2. 角色提取初始化
        if self.path == "/api/init":
            raw_context = req_data.get("context", "")
            custom_name = req_data.get("custom_name", "").strip()
            analyzer_rule = get_prompt_file("persona_analyzer.md")

            sys_prompt = f"""深度分析材料提炼人设。参考规范：
{analyzer_rule}
输出合法纯 JSON：
{{"name": "昵称", "personality": "性格核心", "tone": "日常说话口癖习惯与字数习惯", "likes": "喜好", "dislikes": "反感雷区", "initial_score": 40}}"""

            res = call_deepseek([
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": raw_context}
            ], json_mode=True)

            persona = extract_json_payload(res)
            if not persona or "name" not in persona:
                persona = {
                    "name": custom_name or "TA",
                    "personality": "慢热温和",
                    "tone": "自然简短",
                    "likes": "真诚轻松的话题",
                    "dislikes": "生硬追问与查岗",
                    "initial_score": 40
                }
            if custom_name:
                persona["name"] = custom_name

            with sessions_lock:
                user_game["persona"] = persona
                user_game["score"] = int(persona.get("initial_score", 40))
                user_game["stage"] = "初识试探"
                user_game["history"] = []
                user_game["key_memories"] = []
                user_game["original_context"] = raw_context
                user_game["deduction_logs"] = []
                user_game["is_over"] = False
                save_session_to_disk(session_id)

            self._send_json({"success": True, "persona": persona, "session_id": session_id})
            return

        # 3. 对话与好感度交互
        if self.path == "/api/chat":
            if user_game.get("is_over", False):
                self._send_json({
                    "reply": "故事已落幕。",
                    "end": True,
                    "favorability": user_game.get("score", 0),
                    "emotion": "平静",
                    "session_id": session_id
                })
                return

            user_msg = req_data.get("message", "").strip()
            if not user_msg:
                self._send_json({"reply": "……", "delta": 0, "emotion": "平静", "favorability": user_game["score"]})
                return

            with sessions_lock:
                user_game.setdefault("history", []).append({"role": "user", "content": user_msg})
                compress_memories_if_needed(user_game)

                conv_rule = get_prompt_file("conversation_engine.md")
                memories_str = "暂无特殊事件"
                if user_game.get("key_memories"):
                    memories_str = "；".join(user_game["key_memories"])

                dynamic_sys_prompt = f"""你扮演角色：{json.dumps(user_game['persona'], ensure_ascii=False)}
引擎规范：{conv_rule}
【长期记忆】：{memories_str}
要求：模仿人设语气口吻。玩家可发送表情包 [发送了表情包: xxx (潜台词: xxx)]。
每次回复必须直接输出合法纯 JSON：
{{"reply": "台词", "fav_delta": 整数分值(如 -2, 0, +3), "emotion": "神态"}}"""

                messages_to_send = [{"role": "system", "content": dynamic_sys_prompt}] + user_game["history"][-10:]

            raw_reply = call_deepseek(messages_to_send, json_mode=True)
            data = extract_json_payload(raw_reply)

            reply = data.get("reply") or (raw_reply if raw_reply else "……")
            delta = data.get("fav_delta") if "fav_delta" in data else data.get("delta", 0)
            emotion = data.get("emotion", "平静")

            try:
                delta = int(delta)
            except Exception:
                delta = 0

            with sessions_lock:
                # 记录好感下降瞬间
                if delta < 0:
                    user_game.setdefault("deduction_logs", []).append({
                        "user_said": user_msg,
                        "delta": delta,
                        "target_reply": reply
                    })

                user_game["score"] = max(0, min(100, user_game["score"] + delta))
                user_game["history"].append({"role": "assistant", "content": reply})
                score = user_game["score"]

                if score < 25:
                    stage = "极度疏离"
                elif score < 50:
                    stage = "日常试探"
                elif score < 75:
                    stage = "关系升温"
                elif score < 90:
                    stage = "暧昧拉扯"
                else:
                    stage = "心意相通"
                user_game["stage"] = stage

                end = False
                end_reason = ""
                if score <= 10:
                    end = True
                    user_game["is_over"] = True
                    end_reason = "【坏结局】对方已开启免打扰，距离感无法逾越。"
                elif score >= 95:
                    end = True
                    user_game["is_over"] = True
                    end_reason = "【好结局】好感已达顶峰，对方主动约你周末单独出去玩！"

                save_session_to_disk(session_id)

            self._send_json({
                "reply": reply,
                "delta": delta,
                "emotion": emotion,
                "favorability": score,
                "stage": stage,
                "end": end,
                "end_reason": end_reason,
                "session_id": session_id
            })
            return

        # 4. 面对现实与物理销毁
        if self.path == "/api/farewell":
            prompt = f"""玩家做出了不可逆的选择：彻底删除这个虚拟角色（你），决定放下执念回归现实。
人设：{json.dumps(user_game.get('persona', {}), ensure_ascii=False)}，好感度：{user_game.get('score', 40)}。
请说出你作为虚拟角色的最后一段道别（肯定玩家走向现实的清醒与勇气，表达释怀和感激，70字内直接输出台词）。"""

            farewell_words = call_deepseek([{"role": "user", "content": prompt}], json_mode=False)
            if not farewell_words:
                farewell_words = "谢谢你这段时间的陪伴。去过好属于你自己的真实人生吧，再见啦。"

            # 标记为已结束，保留诊断分析所需语料，删除磁盘文件
            with sessions_lock:
                user_game["is_over"] = True
                disk_file = os.path.join(DATA_DIR, f"{session_id}.json")
                if os.path.exists(disk_file):
                    os.remove(disk_file)

            self._send_json({"farewell": farewell_words.strip()})
            return

        # 5. 终局深度复盘诊断报告
        if self.path == "/api/analyze_report":
            original_ctx = user_game.get("original_context", "（玩家未提供详细记录，仅有基础性格）")
            history_data = user_game.get("history", [])
            persona_data = user_game.get("persona", {})
            deductions = user_game.get("deduction_logs", [])
            final_score = user_game.get("score", 40)

            advisor_rule = get_prompt_file("advisor_reality.md") or get_prompt_file("advisor_report.md")

            report_prompt = f"""你是一名极具洞察力、客观、温和且一针见血的情感分析专家。
玩家刚刚结束了与虚拟角色【{persona_data.get('name', 'TA')}】的所有交互。
参考规范：{advisor_rule}

【材料一：玩家开局提供的最初真实聊天记录 / 描述】
{original_ctx[:6000]}

【材料二：本次模拟交互记录】
{json.dumps(history_data[-14:], ensure_ascii=False)}

【材料三：模拟中扣除好感度的节点记录】
{json.dumps(deductions, ensure_ascii=False)}

【最终好感分值】：{final_score}

请客观剖析，输出纯合法 JSON，必须严格包含以下字段：
{{
    "real_analysis": {{
        "turning_point": "现实中感情发生转折/降温的核心时刻或信号（她在哪里就已经变了）",
        "inner_thoughts": "结合语料细节，剖析TA当时在现实聊天里的真实心理防线与未言之意",
        "real_pattern": "你们在真实相处中存在的根本性格与沟通错位"
    }},
    "sim_analysis": {{
        "performance_eval": "玩家在本次模拟交互中的整体沟通风格评价",
        "critical_mistakes": [
            {{
                "player_quote": "玩家在模拟里引起不适的原话或行为",
                "score_drop": "扣分值",
                "why_she_felt_bad": "TA为什么在这个瞬间感到下头、被冒犯或感到压力"
            }}
        ]
    }},
    "final_verdict": {{
        "relationship_nature": "一句话给这段关系定性（如：错位的单向投射 / 彼此消耗的内耗拉扯等）",
        "reality_advice": "给玩家面对现实、走出执念的清醒真诚寄语（80~120字）"
    }}
}}"""

            raw_report = call_deepseek([{"role": "user", "content": report_prompt}], json_mode=True, timeout=60)
            report_json = extract_json_payload(raw_report)

            if not report_json or "real_analysis" not in report_json:
                report_json = {
                    "real_analysis": {
                        "turning_point": "当日常分享变成单方面的试探时，距离感已经悄然拉开。",
                        "inner_thoughts": "对方感受到了超出舒适界限的压力，用字数缩减来表达防卫。",
                        "real_pattern": "一方渴望确认，另一方习惯空间，步调从未同频。"
                    },
                    "sim_analysis": {
                        "performance_eval": "依然容易将全部期待倾注在对方的每次微小反馈上。",
                        "critical_mistakes": [
                            {
                                "player_quote": "急于确认关系或打探行踪",
                                "score_drop": "-3",
                                "why_she_felt_bad": "过早施加压力，突破了个体边界感。"
                            }
                        ]
                    },
                    "final_verdict": {
                        "relationship_nature": "一段执念深重却注定在错位时空中消耗的独角戏",
                        "reality_advice": "虚拟的回响再逼真，也是投射出来的影子。放下追问‘为什么她不爱我’，去在真实生活里重新找回自己的重心。"
                    }
                }

            # 报告完成后彻底抹除内存会话
            with sessions_lock:
                if session_id in sessions:
                    del sessions[session_id]

            self._send_json({"success": True, "report": report_json})
            return

        # 6. 导出存档
        if self.path == "/api/export":
            with sessions_lock:
                export_data = user_game.copy()
            self._send_json({"success": True, "data": export_data})
            return

        # 7. 导入存档
        if self.path == "/api/import":
            imported_game = req_data.get("save_data", {})
            if not imported_game.get("persona"):
                self._send_json({"success": False, "msg": "存档格式不正确"})
                return

            new_sid = str(uuid.uuid4())
            with sessions_lock:
                sessions[new_sid] = imported_game
                save_session_to_disk(new_sid)

            self._send_json({"success": True, "session_id": new_sid, "data": imported_game})
            return

        self.send_error(404, "API Not Found")

    def _send_json(self, data_dict):
        body_bytes = json.dumps(data_dict, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body_bytes)

def run_server():
    server_address = ("0.0.0.0", PORT)
    httpd = ThreadingHTTPServer(server_address, MultiUserGameHandler)
    print("=" * 60, flush=True)
    print(">>> 聊天模拟引擎已全面启动（原生零依赖 + 记忆持久化 + 终局双轨诊断）", flush=True)
    print(f">>> 浏览器访问地址: http://localhost:{PORT}", flush=True)
    print(f">>> 或输入: [http://127.0.0.1](http://127.0.0.1):{PORT}", flush=True)
    print("=" * 60, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已关闭。")
        httpd.server_close()

if __name__ == '__main__':
    run_server()