import os
import json
from openai import OpenAI

class LoveGameEngine:
    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com"):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = "deepseek-chat"
        
        # 游戏状态机
        self.persona = {}             # 人设卡
        self.favorability = 50        # 好感度 (0-100)
        self.stage = "初识试探"        # 当前阶段
        self.turn_count = 0           # 当前对话轮次
        self.max_turns = 25           # 上限轮次
        self.history = []             # 对话历史
        self.is_over = False          # 结局标志

    def _read_prompt(self, filename: str) -> str:
        path = os.path.join("prompts", filename)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def initialize_character(self, user_text: str):
        """人设提炼建档"""
        analyzer_rule = self._read_prompt("persona_analyzer.md")
        system_prompt = f"""
        你是一名角色提取大师。基于以下参考规范：
        {analyzer_rule}
        
        请分析玩家提供的材料，输出纯 JSON 格式：
        {{
            "name": "TA的昵称",
            "personality": "性格核心词",
            "tone": "日常说话语气习惯（口癖/字数习惯）",
            "likes": "喜好话题",
            "dislikes": "反感雷区",
            "initial_score": 40
        }}
        """
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text}
            ],
            response_format={"type": "json_object"},
            temperature=0.3
        )
        self.persona = json.loads(response.choices[0].message.content)
        self.favorability = self.persona.get("initial_score", 40)
        
        # 设定角色规则
        engine_rule = self._read_prompt("conversation_engine.md")
        roleplay_sys = f"""
        你现在必须完全代入扮演该女生：
        人设：{json.dumps(self.persona, ensure_ascii=False)}
        参考规范：
        {engine_rule}
        
        规则：必须输出合法 JSON 格式：
        {{
            "reply": "你的回复台词",
            "fav_delta": 整数分值（好感度增减，如 -3, 0, +4）,
            "emotion": "当前神态（如：冷漠、傲娇、害羞、敷衍、开心）"
        }}
        """
        self.history.append({"role": "system", "content": roleplay_sys})
        return self.persona

    def play_turn(self, player_message: str) -> dict:
        """单轮游戏交互"""
        if self.is_over:
            return {"reply": "游戏已结束。", "end": True}

        self.turn_count += 1
        self.history.append({"role": "user", "content": player_message})

        response = self.client.chat.completions.create(
            model=self.model,
            messages=self.history,
            response_format={"type": "json_object"},
            temperature=0.7
        )
        
        data = json.loads(response.choices[0].message.content)
        reply = data.get("reply", "")
        delta = data.get("fav_delta", 0)
        emotion = data.get("emotion", "平静")

        self.favorability = max(0, min(100, self.favorability + delta))
        self.history.append({"role": "assistant", "content": reply})

        # 阶段跳转
        if self.favorability < 30:
            self.stage = "极度疏离"
        elif self.favorability < 60:
            self.stage = "日常试探"
        elif self.favorability < 85:
            self.stage = "关系升温"
        else:
            self.stage = "暧昧临界点"

        # 结局判定
        end_reason = None
        if self.favorability <= 10:
            self.is_over = True
            end_reason = "【坏结局】：对方开启了免打扰，你已被拉开距离。"
        elif self.favorability >= 90:
            self.is_over = True
            end_reason = "【好结局】：心动信号！对方主动约你周末出去玩！"
        elif self.turn_count >= self.max_turns:
            self.is_over = True
            end_reason = f"【平局】：回合用尽，好感停留在了 {self.favorability} 分。"

        return {
            "reply": reply,
            "delta": delta,
            "emotion": emotion,
            "favorability": self.favorability,
            "stage": self.stage,
            "turn": f"{self.turn_count}/{self.max_turns}",
            "end": self.is_over,
            "end_reason": end_reason
        }