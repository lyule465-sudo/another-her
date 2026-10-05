import os
import re
import json
import uuid
import time
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# ==================== 基础配置 ====================
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

    replies_m = re.search(r'"replies"\s*:\s*\[([\s\S]*?)\]', text)
    if replies_m:
        try:
            res["replies"] = json.loads(f"[{replies_m.group(1)}]")
        except Exception:
            pass
            
    thought_m = re.search(r'"true_thought"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.DOTALL)
    if thought_m:
        try:
            res["true_thought"] = json.loads(f'"{thought_m.group(1)}"')
        except Exception:
            res["true_thought"] = thought_m.group(1).replace('\\"', '"').replace('\\n', '\n')

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
        "temperature": 0.88
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
    except urllib.error.HTTPError as e:
        err_msg = f"[API错误 {e.code}]: {e.reason}"
        print(err_msg, flush=True)
        return json.dumps({
            "replies": [f"（系统提示: API调用失败 {e.code}，请检查Key与余额）"],
            "true_thought": "系统调用失败",
            "fav_delta": 0,
            "emotion": "异常"
        })
    except Exception as e:
        print(f"[API异常]: {e}", flush=True)
        return json.dumps({
            "replies": ["（网络有点卡，等我一下...）"],
            "true_thought": "网络连接超时",
            "fav_delta": 0,
            "emotion": "超时"
        })

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

    extract_prompt = f"""提炼对话关键约定、生活事实与心防底线：
{json.dumps(old_slice, ensure_ascii=False)}
纯合法 JSON 返回：{{"facts": ["事实1", "事实2"]}}"""

    raw = call_deepseek([{"role": "user", "content": extract_prompt}], json_mode=True)
    res = extract_json_payload(raw)
    facts = res.get("facts", [])
    if facts and isinstance(facts, list):
        user_game.setdefault("key_memories", []).extend(facts)
        if len(user_game["key_memories"]) > 16:
            user_game["key_memories"] = user_game["key_memories"][-16:]

# ==================== 请求路由 ====================
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

        # 2. 角色提炼
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
                addon_traits.append(f"称谓: {gender}")
            if birth_date and zodiac:
                addon_traits.append(f"出生: {birth_date}（{zodiac}）")
            if mbti:
                addon_traits.append(f"MBTI: {mbti}")
            addon_str = "；".join(addon_traits) if addon_traits else "依赖对话记录分析"

            sys_prompt = f"""深度剖析真实语料，提炼该角色的真实沟通灵魂与文字习惯。参考规范：{analyzer_rule}
补充基底：{addon_str}
核心要求：
1. 找出TA在微信里【聊天主动性】是怎样的（是爱主动分享、还是喜欢反问对方、还是慢热）。
2. 找出TA习惯的【口癖与断句方式】（比如连发两句短话，还是大段文字）。
3. 找出TA【不想回答或尴尬时】的借口套路与真实心理防卫。
输出纯合法 JSON：
{{
    "name": "昵称或名字",
    "personality": "性格底层",
    "tone": "说话口癖与回复长度习惯",
    "proactivity_style": "主动挑起话题或反问的习惯风格",
    "likes": "喜好话题",
    "dislikes": "反感雷区",
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
                    "personality": "注重自我边界，慢热且有真实生活节律",
                    "tone": "短句碎片为主，随性自然，极少用句号",
                    "proactivity_style": "偶尔会反问对方的生活，或者随口吐槽自己遇到的事",
                    "likes": "轻松愉快的生活细节、不带压迫感的互动",
                    "dislikes": "查岗式追问、频繁打探行踪、说教",
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

        # 3. 拟真对话交互（短句连发 + 拒绝单向应答 + 主动反问）
        if self.path == "/api/chat":
            if user_game.get("is_over", False):
                self._send_json({
                    "replies": ["故事已经落幕了。"],
                    "true_thought": "这段关系已经彻底画上句号了。",
                    "end": True,
                    "favorability": user_game.get("score", 0),
                    "emotion": "释怀",
                    "session_id": session_id
                })
                return

            user_msg = req_data.get("message", "").strip()
            if not user_msg:
                self._send_json({"replies": ["？"], "true_thought": "怎么发了空白？", "delta": 0, "emotion": "疑惑", "favorability": user_game["score"]})
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
                extra_bg = f"星座: {p.get('zodiac', '未知')} | MBTI: {p.get('mbti', '未知')} | 称谓: {p.get('gender', 'TA')}"

                alive_human_directives = """【彻底摒弃 AI 机器感、让角色活过来的核心铁律】：
1. 【拒绝只当单向应答机】：
   - 严禁“你问我答”的客服模式！现实人类在聊天时是充满主动性的。
   - 只要契机合适，你必须【主动反问】对方（例如：“你今天怎么突然问这个？”、“你那边还不睡啊？”、“你呢？”）、【主动吐槽】或者【带出自己此刻手头正在做的现实琐事】（如赶报告、喝水、打游戏、看视频）。
2. 【微信碎片化短句连发】：
   - 真实人类发微信是“打碎了发”的。请在 replies 数组里返回 1~3 条自然的微信短句（例如：["刚看到消息", "刚才在洗头哈哈", "怎么啦突然问这个"]）。
   - 避免把所有话写成一个又长又工整的长句。
3. 【true_thought 绝不对玩家撒谎】：
   - 无论表面上多么客套、找借口、逃避或调侃，内心独白必须 100% 毫无保留地交代真实想法、对提问的真正答案与心理防线。"""

                dynamic_sys_prompt = f"""你是真实世界里的【{p.get('name', 'TA')}】。
人设底核：{json.dumps(p, ensure_ascii=False)}
背景参考：{extra_bg}
彼此经历：{memories_str}
当前好感：{current_score}/100（当前阶段：{prev_stage}）
玩家刚发来消息：「{user_msg}」

{alive_human_directives}
规范准则：{conv_rule}

必须直接输出纯合法 JSON：
{{
    "true_thought": "TA内心深处毫无伪装的真实想法、对提问的真正答案与动机（绝不对玩家隐瞒，40-90字）",
    "replies": ["微信短句1", "微信短句2(可选，主动反问或补充细节)", "微信短句3(可选)"],
    "fav_delta": 好感增减整数(-5 到 +4),
    "emotion": "当前微神态(如：心虚、防卫、好奇、轻笑、走神)"
}}"""

                messages_to_send = [{"role": "system", "content": dynamic_sys_prompt}] + user_game["history"][-10:]

            raw_reply = call_deepseek(messages_to_send, json_mode=True)
            data = extract_json_payload(raw_reply)

            replies = data.get("replies")
            if not replies or not isinstance(replies, list):
                single = data.get("reply") or (re.sub(r'[{}\[\]"]', '', raw_reply).strip() if raw_reply else "")
                replies = [single if single else "怎么啦？"]

            true_thought = data.get("true_thought", "（对方此刻内心有所防备，未曾向你言说真实想法）")
            delta = data.get("fav_delta") if "fav_delta" in data else data.get("delta", 0)
            emotion = data.get("emotion", "平静")

            try:
                delta = int(delta)
            except Exception:
                delta = 0

            with sessions_lock:
                deduction_entry = None
                combined_reply_str = " ".join(replies)
                if delta < 0:
                    deduction_entry = {
                        "user_said": user_msg,
                        "delta": delta,
                        "target_reply": combined_reply_str,
                        "inner_truth": true_thought
                    }
                    user_game.setdefault("deduction_logs", []).append(deduction_entry)

                user_game["score"] = max(0, min(100, user_game["score"] + delta))
                user_game["history"].append({
                    "role": "assistant",
                    "content": combined_reply_str,
                    "replies": replies,
                    "true_thought": true_thought
                })
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
                    end_reason = "对方开启了免打扰，隔阂已经无法跨越。"
                elif score >= 95:
                    end = True
                    user_game["is_over"] = True
                    end_reason = "心意已达顶峰，对方主动邀请了现实见面。"

                save_session_to_disk(session_id)

            self._send_json({
                "replies": replies,
                "true_thought": true_thought,
                "delta": delta,
                "emotion": emotion,
                "favorability": score,
                "stage": stage,
                "end": end,
                "end_reason": end_reason,
                "session_id": session_id
            })
            return

        # 3.1 角色主动发消息 / 戳一戳触发
        if self.path == "/api/proactive":
            if user_game.get("is_over", False):
                self._send_json({"replies": ["（已结束交互）"], "true_thought": "无", "emotion": "平静"})
                return

            with sessions_lock:
                p = user_game.get("persona", {})
                current_score = user_game.get("score", 40)
                memories_str = "；".join(user_game.get("key_memories", [])) if user_game.get("key_memories") else "暂无"
                extra_bg = f"星座: {p.get('zodiac', '未知')} | MBTI: {p.get('mbti', '未知')}"

                proactive_prompt = f"""你就是真实世界里的【{p.get('name', 'TA')}】。
人设内核：{json.dumps(p, ensure_ascii=False)}
特质：{extra_bg}
共同经历：{memories_str}
当前好感度：{current_score}/100

【情景】：
过了片刻，你拿着手机，决定【主动】发消息找玩家。
请像现实中真人一样，主动发一条或两条微信：
- 可以是主动问对方在干嘛、或者随口提起今天的一件小事/搞笑的事。
- 如果好感偏低（<35），语气随意冷淡一些，比如：“你刚才问那个干嘛”或“在忙吗”。
- 如果好感正常或偏高（>60），分享欲会明显增加，主动抛出共同话题或反问对方。
- 微信短句连发（1~2句）。

输出纯合法 JSON：
{{
    "true_thought": "TA主动发消息时的内心深层活动（不撒谎）",
    "replies": ["主动短句1", "主动短句2(可选)"],
    "emotion": "主动发起对话时的状态"
}}"""
                messages_to_send = [{"role": "system", "content": proactive_prompt}] + user_game["history"][-8:]

            raw_reply = call_deepseek(messages_to_send, json_mode=True)
            data = extract_json_payload(raw_reply)

            replies = data.get("replies")
            if not replies or not isinstance(replies, list):
                single = data.get("reply") or "在干嘛呢"
                replies = [single]

            true_thought = data.get("true_thought", "闲下来想起来看了一眼手机。")
            emotion = data.get("emotion", "随性")

            with sessions_lock:
                combined_reply_str = " ".join(replies)
                user_game["history"].append({
                    "role": "assistant",
                    "content": combined_reply_str,
                    "replies": replies,
                    "true_thought": true_thought
                })
                save_session_to_disk(session_id)

            self._send_json({
                "replies": replies,
                "true_thought": true_thought,
                "emotion": emotion,
                "favorability": user_game["score"],
                "stage": user_game["stage"]
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

        # 5. 面对现实
        if self.path == "/api/farewell":
            p = user_game.get('persona', {})
            prompt = f"""玩家选择面对现实，彻底销毁虚拟角色。
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

        # 6. 终局复盘报告
        if self.path == "/api/analyze_report":
            original_ctx = user_game.get("original_context", "（未提供历史材料）")
            history_data = user_game.get("history", [])
            p = user_game.get("persona", {})
            deductions = user_game.get("deduction_logs", [])
            final_score = user_game.get("score", 40)

            advisor_rule = get_prompt_file("advisor_reality.md") or get_prompt_file("advisor_report.md")

            report_prompt = f"""你是一名极具深度洞察力的情感顾问。
玩家刚刚结束了与虚拟角色【{p.get('name', 'TA')}】的模拟。
对象基底：星座【{p.get('zodiac', '未知')}】，MBTI【{p.get('mbti', '未知')}】，称谓【{p.get('gender', 'TA')}】。
参考指引：{advisor_rule}

【材料一：原始真实记录】
{original_ctx[:6000]}

【材料二：模拟对话与内心真实独白】
{json.dumps(history_data[-14:], ensure_ascii=False)}

【材料三：扣分与心理抗拒瞬间】
{json.dumps(deductions, ensure_ascii=False)}

【最终得分】：{final_score}

请结合其星座的防御本能、MBTI模式，结合真实语料与模拟，剖析真实心理。
纯合法 JSON 返回：
{{
    "real_analysis": {{
        "turning_point": "现实中感情发生降温的核心转折点",
        "inner_thoughts": "结合星座与真实材料，剖析TA在现实中未曾言说的防备、心理边界与压力源",
        "real_pattern": "两人在现实中最根本的性格错位"
    }},
    "sim_analysis": {{
        "performance_eval": "玩家在本次模拟中的互动模式与执念表现",
        "critical_mistakes": [
            {{
                "player_quote": "模拟中引起对方戒备或不适的原话",
                "score_drop": "扣分值",
                "why_she_felt_bad": "结合真实心理，剖析TA在此刻感到压迫或逃避的真正原因"
            }}
        ]
    }},
    "final_verdict": {{
        "relationship_nature": "一针见血的关系定性",
        "reality_advice": "给玩家面对现实、走出执念的治愈寄语（80~120字）"
    }}
}}"""

            raw_report = call_deepseek([{"role": "user", "content": report_prompt}], json_mode=True, timeout=60)
            report_json = extract_json_payload(raw_report)

            if not report_json or "real_analysis" not in report_json:
                report_json = {
                    "real_analysis": {
                        "turning_point": "当日常分享变成单方面的试探时，距离感已经拉开。",
                        "inner_thoughts": "感知到了越界的压力，用敷衍和沉默来保护个人边界。",
                        "real_pattern": "一个渴望确认，一个需要空间，步调从未同频。"
                    },
                    "sim_analysis": {
                        "performance_eval": "依然容易将全部期待倾注在对方的每次微弱反馈上。",
                        "critical_mistakes": [
                            {
                                "player_quote": "连续追问原因或打探心意",
                                "score_drop": "-3",
                                "why_she_felt_bad": "迫使对方在没有准备好的情况下表态，形成了心理压迫。"
                            }
                        ]
                    },
                    "final_verdict": {
                        "relationship_nature": "一段在错位时空里被反复重播的单向投射",
                        "reality_advice": "屏幕里的回音再逼真，也是投射出来的影子。放下追问‘TA到底爱不爱我’，去在现实生活里找回自己的重心。"
                    }
                }

            with sessions_lock:
                if session_id in sessions:
                    del sessions[session_id]

            self._send_json({"success": True, "report": report_json})
            return

        # 7. 导出
        if self.path == "/api/export":
            with sessions_lock:
                export_data = user_game.copy()
            self._send_json({"success": True, "data": export_data})
            return

        # 8. 导入
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
    print(">>> 《另一个她/他》活体双轨引擎启动", flush=True)
    print(f">>> 端口: {PORT} (http://localhost:{PORT})", flush=True)
    print("=" * 60, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
        httpd.server_close()

if __name__ == '__main__':
    run_server()
