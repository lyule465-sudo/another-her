import os
import re
import json
import uuid
import time
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# ==================== 配置区 ====================
MY_API_KEY = "sk-1d476b0c3c5a44fc88b9335f5f47e4a8"
API_URL = "https://api.deepseek.com/chat/completions"
PORT = int(os.environ.get("PORT", 8080))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data", "sessions")
os.makedirs(DATA_DIR, exist_ok=True)
os.chdir(BASE_DIR)

sessions_lock = threading.Lock()
sessions = {}

# ==================== 持久化存储与容错 ====================
def save_session_to_disk(sid):
    file_path = os.path.join(DATA_DIR, f"{sid}.json")
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(sessions[sid], f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[存档写入异常 {sid}]: {e}", flush=True)

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
            "stage": "日常试探",
            "key_memories": [],
            "history": [],
            "snapshots": [],
            "original_context": "",
            "deduction_logs": [],
            "is_over": False
        }
        save_session_to_disk(new_id)
        return new_id, sessions[new_id]

def extract_json_payload(raw_text: str) -> dict:
    """强化版 JSON 提取器：杜绝非转义换行或标签污染引发的丢话 Bug"""
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
        return json.loads(text, strict=False)
    except Exception:
        pass
    
    res = {}
    reply_m = re.search(r'"reply"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.DOTALL)
    if reply_m:
        try:
            res["reply"] = json.loads(f'"{reply_m.group(1)}"')
        except Exception:
            res["reply"] = reply_m.group(1).replace('\\"', '"').replace('\\n', '\n')
            
    delta_m = re.search(r'"(?:fav_delta|delta)"\s*:\s*(-?\d+)', text)
    if delta_m:
        res["fav_delta"] = int(delta_m.group(1))
        
    emo_m = re.search(r'"emotion"\s*:\s*"([^"]+)"', text)
    if emo_m:
        res["emotion"] = emo_m.group(1)
        
    return res

def call_deepseek(messages, json_mode=False, timeout=45):
    payload = {
        "model": "deepseek-chat",
        "messages": messages,
        "temperature": 0.8
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
        print(f"[API 交互异常]: {e}", flush=True)
        return None

def get_prompt_file(filename):
    p = os.path.join(BASE_DIR, "prompts", filename)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    return ""

def compress_memories_if_needed(user_game):
    if len(user_game.get("history", [])) < 16:
        return
    old_slice = user_game["history"][:6]
    user_game["history"] = user_game["history"][6:]

    extract_prompt = f"""提炼以下对话中涉及的二人重要经历、承诺、生活习惯或秘密线索：
{json.dumps(old_slice, ensure_ascii=False)}
纯合法 JSON 返回：{{"facts": ["事实1", "事实2"]}}"""

    raw = call_deepseek([{"role": "user", "content": extract_prompt}], json_mode=True)
    res = extract_json_payload(raw)
    facts = res.get("facts", [])
    if facts and isinstance(facts, list):
        user_game.setdefault("key_memories", []).extend(facts)
        if len(user_game["key_memories"]) > 16:
            user_game["key_memories"] = user_game["key_memories"][-16:]

# ==================== 请求路由器 ====================
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

        # 1. 恢复存档
        if self.path == "/api/load_session":
            has_role = bool(user_game.get("persona") and user_game["persona"].get("name"))
            self._send_json({
                "has_role": has_role,
                "persona": user_game.get("persona", {}),
                "score": user_game.get("score", 40),
                "stage": user_game.get("stage", "日常试探"),
                "history": user_game.get("history", []),
                "session_id": session_id,
                "is_over": user_game.get("is_over", False)
            })
            return

        # 2. 角色提炼与仿真人设构建
        if self.path == "/api/init":
            raw_context = req_data.get("context", "")
            custom_name = req_data.get("custom_name", "").strip()
            birth_date = req_data.get("birth_date", "").strip()
            zodiac = req_data.get("zodiac", "").strip()
            mbti = req_data.get("mbti", "").strip()
            gender = req_data.get("gender", "她").strip()
            avatar_url = req_data.get("avatar_url", "").strip()

            analyzer_rule = get_prompt_file("persona_analyzer.md")

            addon_traits = []
            if gender:
                addon_traits.append(f"生理性别/身份称谓: {gender}")
            if birth_date and zodiac:
                addon_traits.append(f"生日: {birth_date}（{zodiac}）")
            if mbti:
                addon_traits.append(f"MBTI 人格框架: {mbti}")
            addon_str = "；".join(addon_traits) if addon_traits else "无指定预设，纯依赖原始聊天记录分析"

            sys_prompt = f"""你是一名资深人际沟通心理学家。请深度剖析以下聊天材料，提炼该角色的真实沟通灵魂与文字生理习惯。
参考指引：{analyzer_rule}
补充基底：{addon_str}

【提炼准则（极度重要）】：
1. 提取对方真实的【标点使用偏好】（如：从来不打句号/喜欢连续空格断句/爱用问号/爱用波浪号~）。
2. 提取【常用口头禅与词汇习惯】（如“哈哈”、“害”、“笑死”、“好滴”、“嗯嗯”还是“嗯”）。
3. 提取【单条消息字数偏好】（是秒发几个字的碎片化短句，还是整段长文）。
4. 结合其星座防卫机制与 MBTI，明确其【核心雷区（反感的压迫/说教/查岗）】与【好感开关】。

必须直接输出纯合法 JSON：
{{
    "name": "昵称或名字",
    "personality": "性格底层（如：慢热但对外随和，骨子里极度看重边界感）",
    "tone": "说话风格（必须说明标点偏好、口头禅、回复字数长短习惯）",
    "likes": "容易引起共鸣的话题或行为",
    "dislikes": "反感或容易冷处理的雷区",
    "initial_score": 40
}}"""

            res = call_deepseek([
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": raw_context}
            ], json_mode=True)

            persona = extract_json_payload(res)
            if not persona or "name" not in persona:
                persona = {
                    "name": custom_name or "TA",
                    "personality": "外表客气随和，内心有极强的个人界限，极度慢热",
                    "tone": "短句为主，极少使用正式句号，常以空格或语气助词结尾",
                    "likes": "松弛的生活分享、不设防的幽默感",
                    "dislikes": "频繁打探行踪、情绪索取与说教式追问",
                    "initial_score": 40
                }
            if custom_name:
                persona["name"] = custom_name
            
            persona["gender"] = gender
            persona["birth_date"] = birth_date
            persona["zodiac"] = zodiac
            persona["mbti"] = mbti
            persona["avatar_url"] = avatar_url

            with sessions_lock:
                user_game["persona"] = persona
                user_game["score"] = int(persona.get("initial_score", 40))
                user_game["stage"] = "日常试探"
                user_game["history"] = []
                user_game["snapshots"] = []
                user_game["key_memories"] = []
                user_game["original_context"] = raw_context
                user_game["deduction_logs"] = []
                user_game["is_over"] = False
                save_session_to_disk(session_id)

            self._send_json({"success": True, "persona": persona, "session_id": session_id})
            return

        # 3. 拟真人格聊天交互
        if self.path == "/api/chat":
            if user_game.get("is_over", False):
                self._send_json({
                    "reply": "故事已画上句号。",
                    "end": True,
                    "favorability": user_game.get("score", 0),
                    "emotion": "释怀",
                    "session_id": session_id
                })
                return

            user_msg = req_data.get("message", "").strip()
            if not user_msg:
                self._send_json({"reply": "……", "delta": 0, "emotion": "发呆", "favorability": user_game["score"]})
                return

            with sessions_lock:
                prev_score = user_game["score"]
                prev_stage = user_game["stage"]
                current_score = prev_score

                user_game.setdefault("history", []).append({"role": "user", "content": user_msg})
                compress_memories_if_needed(user_game)

                conv_rule = get_prompt_file("conversation_engine.md")
                memories_str = "暂无特殊事件"
                if user_game.get("key_memories"):
                    memories_str = "；".join(user_game["key_memories"])

                p = user_game["persona"]
                extra_bg = f"星座: {p.get('zodiac', '未知')} | MBTI: {p.get('mbti', '未知')} | 身份: {p.get('gender', 'TA')}"

                human_directive = """【脱离 AI 感的核心仿真指令】：
1. 绝对不要像智能助手那样体贴周到、问一答十！现实人类聊天是充满随机性、防御性和生活碎片的。
2. 严格执行人设的【标点与字数习惯】：如果人设习惯打短句，绝不输出长段落；如果语料中很少用句号，就用空格或不加标点。
3. 情绪温差反馈：
   - 当好感低或玩家提问冒犯时：表现出冷淡、防备、惜字如金（例如回：“还行吧”、“没干嘛”、“？”、“哦哦”），绝不假装热情配合。
   - 当好感高或话题投缘时：语气才更生动，会主动追问或分享自己的事情。
4. 玩家可能发表情包：[发送了表情包: 名称 (潜台词: 含义)]，请根据两人当前关系阶段自然应对。"""

                dynamic_sys_prompt = f"""你就是真实世界里的【{p.get('name', 'TA')}】。
人设内核：{json.dumps(p, ensure_ascii=False)}
背景参考：{extra_bg}
共同经历记忆：{memories_str}
当前对玩家的好感度数值：{current_score}/100（当前阶段：{prev_stage}）
{human_directive}
规范准则：{conv_rule}

输出纯合法 JSON：
{{
    "reply": "完全符合人设日常发信息的真实回复",
    "fav_delta": 整数好感增减(-5 到 +4 之间，无明显情感波动请给 0),
    "emotion": "当前微神态(如：玩手机、敷衍、好奇、轻笑、防御)"
}}"""

                messages_to_send = [{"role": "system", "content": dynamic_sys_prompt}] + user_game["history"][-10:]

            raw_reply = call_deepseek(messages_to_send, json_mode=True)
            data = extract_json_payload(raw_reply)

            reply = data.get("reply")
            if not reply:
                cleaned = re.sub(r'[{}\[\]"]', '', raw_reply).strip() if raw_reply else ""
                reply = cleaned if cleaned else "……"

            delta = data.get("fav_delta") if "fav_delta" in data else data.get("delta", 0)
            emotion = data.get("emotion", "平静")

            try:
                delta = int(delta)
            except Exception:
                delta = 0

            with sessions_lock:
                deduction_entry = None
                if delta < 0:
                    deduction_entry = {
                        "user_said": user_msg,
                        "delta": delta,
                        "target_reply": reply
                    }
                    user_game.setdefault("deduction_logs", []).append(deduction_entry)

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

                user_game.setdefault("snapshots", []).append({
                    "score_before": prev_score,
                    "stage_before": prev_stage,
                    "had_deduction": bool(deduction_entry)
                })

                end = False
                end_reason = ""
                if score <= 10:
                    end = True
                    user_game["is_over"] = True
                    end_reason = "对方设置了免打扰，未回覆的消息停在了屏幕上。"
                elif score >= 95:
                    end = True
                    user_game["is_over"] = True
                    end_reason = "心意已达顶峰，对方主动发来了见面的具体时间。"

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

        # 4. 撤回上一句
        if self.path == "/api/rollback":
            with sessions_lock:
                if not user_game.get("snapshots") or len(user_game.get("history", [])) < 2:
                    self._send_json({"success": False, "msg": "当前没有可撤回的对话"})
                    return
                
                last_snap = user_game["snapshots"].pop()
                user_game["score"] = last_snap["score_before"]
                user_game["stage"] = last_snap["stage_before"]
                
                user_game["history"] = user_game["history"][:-2]
                
                if last_snap.get("had_deduction") and user_game.get("deduction_logs"):
                    user_game["deduction_logs"].pop()
                
                user_game["is_over"] = False
                save_session_to_disk(session_id)

            self._send_json({
                "success": True,
                "score": user_game["score"],
                "stage": user_game["stage"]
            })
            return

        # 5. 面对现实终局
        if self.path == "/api/farewell":
            p = user_game.get('persona', {})
            prompt = f"""玩家做出了清醒勇敢的决定：主动删除该角色的所有数据，走出虚拟回响，面对现实生活。
角色人设：{json.dumps(p, ensure_ascii=False)}，最终好感：{user_game.get('score', 40)}。
请你以该角色的真实语气，说出最后一句真诚、克制而释怀的告别（认可对方走向现实的勇气，祝愿TA好好生活，70字内，直接输出台词）。"""

            farewell_words = call_deepseek([{"role": "user", "content": prompt}], json_mode=False)
            if not farewell_words:
                farewell_words = "谢谢你这段时间的陪伴。去过好属于你自己的真实人生吧，再见啦。"

            with sessions_lock:
                user_game["is_over"] = True
                disk_file = os.path.join(DATA_DIR, f"{session_id}.json")
                if os.path.exists(disk_file):
                    os.remove(disk_file)

            self._send_json({"farewell": farewell_words.strip()})
            return

        # 6. 终局深度心理诊断报告
        if self.path == "/api/analyze_report":
            original_ctx = user_game.get("original_context", "（未提供历史材料）")
            history_data = user_game.get("history", [])
            p = user_game.get("persona", {})
            deductions = user_game.get("deduction_logs", [])
            final_score = user_game.get("score", 40)

            advisor_rule = get_prompt_file("advisor_reality.md") or get_prompt_file("advisor_report.md")

            report_prompt = f"""你是一名犀利、温和且具有极高心理洞察力的情感顾问。
玩家刚刚结束了与虚拟角色【{p.get('name', 'TA')}】的所有模拟。
对象基底：星座【{p.get('zodiac', '未知')}】，MBTI【{p.get('mbti', '未知')}】，身份【{p.get('gender', 'TA')}】。
参考指引：{advisor_rule}

【材料一：玩家输入的真实过往记录与背景】
{original_ctx[:6000]}

【材料二：本次模拟后期的关键交互】
{json.dumps(history_data[-14:], ensure_ascii=False)}

【材料三：模拟中触发扣分的具体瞬间】
{json.dumps(deductions, ensure_ascii=False)}

【终局好感评分】：{final_score}

请结合其星座的防御本能、MBTI认知模式、真实语料细节与模拟中表现，做出一份让人释怀的深度复盘。
纯合法 JSON 返回：
{{
    "real_analysis": {{
        "turning_point": "现实中感情发生转折/温度骤降的标志性节点",
        "inner_thoughts": "结合星座与真实材料，剖析TA在现实未曾言说的防备、心理边界与压力源",
        "real_pattern": "两人在真实沟通中最致命的错位节奏"
    }},
    "sim_analysis": {{
        "performance_eval": "玩家在本次模拟中的互动模式与执念表现",
        "critical_mistakes": [
            {{
                "player_quote": "模拟中引起对方不适或冷淡的原话",
                "score_drop": "扣分值",
                "why_she_felt_bad": "结合MBTI特质，剖析TA在此刻感到压迫、索取或下头的真正心理原因"
            }}
        ]
    }},
    "final_verdict": {{
        "relationship_nature": "一针见血的关系定性（如：一场在错位时空的单向执念投射）",
        "reality_advice": "给玩家面对现实、爱护自己、放下心结的治愈寄语（80~120字）"
    }}
}}"""

            raw_report = call_deepseek([{"role": "user", "content": report_prompt}], json_mode=True, timeout=60)
            report_json = extract_json_payload(raw_report)

            if not report_json or "real_analysis" not in report_json:
                report_json = {
                    "real_analysis": {
                        "turning_point": "当日常的随意分享演变成单方面的小心试探时，温度早已悄然改变。",
                        "inner_thoughts": "对方感知到了超越舒适区的重量，文字变短只是本能的防卫机制。",
                        "real_pattern": "一个习惯了索取确认，一个本能地需要独处空间，始终不在一个频段。"
                    },
                    "sim_analysis": {
                        "performance_eval": "依然容易把自身的情绪寄托在对方微弱的回应上。",
                        "critical_mistakes": [
                            {
                                "player_quote": "急切地追问对方在干嘛或暗示关系",
                                "score_drop": "-3",
                                "why_she_felt_bad": "无形中形成了人际施压，突破了个体应有的边界感。"
                            }
                        ]
                    },
                    "final_verdict": {
                        "relationship_nature": "一段在错位时空里被反复重播的单向投射",
                        "reality_advice": "屏幕里的回音再真实，也是心底未解执念的投影。去真实世界里拥抱晒得到太阳的生活，你值得一份不需要猜忌与消耗的爱。"
                    }
                }

            with sessions_lock:
                if session_id in sessions:
                    del sessions[session_id]

            self._send_json({"success": True, "report": report_json})
            return

        # 7. 导出存档
        if self.path == "/api/export":
            with sessions_lock:
                export_data = user_game.copy()
            self._send_json({"success": True, "data": export_data})
            return

        # 8. 导入存档
        if self.path == "/api/import":
            imported_game = req_data.get("save_data", {})
            if not imported_game.get("persona"):
                self._send_json({"success": False, "msg": "存档结构异常"})
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
    print(">>> 《另一个她/他》拟真交互引擎已就绪", flush=True)
    print(f">>> 服务端口: {PORT} (http://localhost:{PORT})", flush=True)
    print("=" * 60, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
        httpd.server_close()

if __name__ == '__main__':
    run_server()
