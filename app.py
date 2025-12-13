import os
import json
import io
import requests
import streamlit as st
import pandas as pd
from datetime import datetime
from bs4 import BeautifulSoup
from openai import OpenAI
from dotenv import load_dotenv
import time
import hashlib
import difflib
from urllib.parse import urlparse

# =========================================================
# 基本設定
# =========================================================
APP_TITLE = "人事評価制度 自動生成AI"
APP_VERSION = "23.2.11"  # 23.2.10修正（json必須/重複回避/進捗安定）＋任意の環境固定ガード

st.set_page_config(page_title=f"{APP_TITLE} v{APP_VERSION}", layout="centered")
load_dotenv()

# CSS: プログレスバー & 品質管理パネル
st.markdown(
    """
    <style>
    .stProgress > div > div > div > div {
        background-image: linear-gradient(to right, #4cd964, #5ac8fa);
        background-color: #4cd964;
    }
    .control-panel {
        background-color: #262730;
        padding: 20px;
        border-radius: 10px;
        border: 1px solid #444;
        margin-bottom: 20px;
    }
    .panel-header {
        font-weight: bold;
        font-size: 1.2em;
        margin-bottom: 15px;
        color: #fff;
        border-bottom: 1px solid #555;
        padding-bottom: 5px;
    }
    .alert-box-warning {
        background-color: #332701;
        border-left: 5px solid #ffc107;
        padding: 10px;
        margin-bottom: 10px;
        color: #fff;
    }
    .alert-box-success {
        background-color: #0e2a10;
        border-left: 5px solid #28a745;
        padding: 10px;
        margin-bottom: 10px;
        color: #fff;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def get_secret(key: str):
    return st.secrets[key] if key in st.secrets else os.getenv(key)


# =========================================================
# 環境ガード（ENV固定：APP_ENV→project_refをコードで拘束）
# =========================================================
APP_ENV = (get_secret("APP_ENV") or "").strip().lower()  # dev/demo/prod
ENV_LABEL = (get_secret("ENV_LABEL") or APP_ENV.upper()).strip()
STRICT_ENV_GUARD = str(get_secret("STRICT_ENV_GUARD") or "true").strip().lower() in ("1", "true", "yes", "on")

# ★ 書き込み導線の封印（UI側の物理ガード）
WRITE_ENABLED = str(get_secret("WRITE_ENABLED") or "false").strip().lower() in ("1", "true", "yes", "on")

# ★ここに「各環境の project_ref」を固定で書く（xxxx.supabase.co の xxxx）
# 優先順位：
# 1) secrets の ENV_REF_DEV/DEMO/PROD が入っていればそれを使う（運用で差し替え可）
# 2) 無ければ、ここに書いた固定値（あなたが提示したproject_ref）を使う
ENV_TO_PROJECT_REF = {
    "dev":  (get_secret("ENV_REF_DEV")  or "xpaktdfzhinbwdchyltf").strip(),
    "demo": (get_secret("ENV_REF_DEMO") or "gwjaxkntwbcvnjubfjoz").strip(),
    "prod": (get_secret("ENV_REF_PROD") or "rrieppgrmutdhytoxekz").strip(),
}
# ※完全に“コード固定”にしたいなら上の get_secret(...) を消して直書きでもOK
# ENV_TO_PROJECT_REF = {"dev":"xpaktdfzhinbwdchyltf","demo":"gwjaxkntwbcvnjubfjoz","prod":"rrieppgrmutdhytoxekz"}


def _extract_supabase_project_ref(url: str) -> str:
    try:
        host = urlparse(url).netloc
        if host.endswith(".supabase.co"):
            return host.split(".")[0]
        return ""
    except Exception:
        return ""


def _env_color(env: str) -> str:
    env = (env or "").lower()
    if env == "prod":
        return "#ff4d4d"
    if env == "dev":
        return "#4da3ff"
    return "#ffb84d"


def env_guard_or_stop():
    if APP_ENV not in ("dev", "demo", "prod"):
        st.error("APP_ENV が未設定または不正です。dev / demo / prod のいずれかにしてください。")
        st.stop()

    st.markdown(
        f"""
        <div style="padding:8px 12px;border-radius:10px;background:{_env_color(APP_ENV)};color:white;
        font-weight:700;display:inline-block;margin-bottom:10px;">
        ENV: {ENV_LABEL}（{APP_ENV}）
        </div>
        """,
        unsafe_allow_html=True,
    )

    sb_url = (get_secret("SUPABASE_URL") or "").strip()
    if not sb_url:
        if STRICT_ENV_GUARD:
            st.error("SUPABASE_URL が未設定です。環境混線を防ぐため起動を停止します。")
            st.stop()
        return

    actual_ref = _extract_supabase_project_ref(sb_url)
    expected_ref = (ENV_TO_PROJECT_REF.get(APP_ENV) or "").strip()

    # ★厳格モードなら dev/demo/prod 全部で expected_ref 未設定は停止（穴を残さない）
    if STRICT_ENV_GUARD and not expected_ref:
        st.error(f"{APP_ENV}環境で project_ref が固定されていません。ENV_TO_PROJECT_REF を設定してください。")
        st.stop()

    # expected_ref が設定されている場合は必ず一致チェック
    if expected_ref:
        if not actual_ref:
            st.error("SUPABASE_URL から project_ref を抽出できません。URL形式を確認してください。")
            st.stop()
        if actual_ref != expected_ref:
            st.error(
                "Supabase接続先が環境と一致しません（誤接続防止のため停止）。\n\n"
                f"- ENV:      {APP_ENV}\n"
                f"- EXPECTED: {expected_ref}\n"
                f"- ACTUAL:   {actual_ref}\n\n"
                "Streamlit secrets の SUPABASE_URL を修正してください。"
            )
            st.stop()


# =========================================================
# Secrets & OpenAI
# =========================================================
SUPABASE_URL = get_secret("SUPABASE_URL")
SUPABASE_KEY = get_secret("SUPABASE_SERVICE_ROLE_KEY")  # ※将来用（現状未使用）
OPENAI_API_KEY = get_secret("OPENAI_API_KEY")
APP_PASSWORD = get_secret("APP_PASSWORD")
FIXED_COMPANY_NAME = get_secret("FIXED_COMPANY_NAME")
FIXED_COMPANY_URL = get_secret("FIXED_COMPANY_URL")

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# =========================================================
# 経営理論バックボーン
# =========================================================
THEORETICAL_BACKBONE = """
【AIが思考プロセスで強制的に参照すべき9人の巨匠とその理論】
1. Philosophy & Strategy: P.F.ドラッカー(貢献), M.ポーター(競争優位), J.コッター(変革)
2. Structure & Balance: R.カプラン(BSC), D.ウルリッチ(戦略HR), M.ヒースリッド(HPWS)
3. Operation & Development: 野中郁次郎(SECI), A.デ・ワール(HPO), T.V.ラオ(人材開発)
""".strip()

# =========================================================
# グローバル設定 & 定数
# =========================================================
USER_PLAN = "standard"

FALLBACK_PHILOSOPHY = """
【重要理念語】自立支援、個別ケア、地域包括ケア、チーム協働、専門性向上。
【目指す状態】利用者様の生活の質の最大化と、職員のやりがいを両立する。
""".strip()

DEFAULT_MAJOR_CATEGORIES = {
    "I": "社会人基礎と職業倫理",
    "II": "理念と経営方針理解",
    "III": "法令遵守と制度理解",
    "IV": "専門職務遂行力",
    "V": "記録・報告・情報管理",
    "VI": "チーム連携・多職種協働",
    "VII": "リスクマネジメントと倫理判断",
    "VIII": "ICT・AI・DX推進",
    "IX": "自己研鑽と地域貢献",
    "X": "福利厚生活用と将来設計",
}

ROMAN_ORDER = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]

USER_DEFINED_LEVELS = {
    "Lv1": {"経験年数": "入社〜1年未満", "想定役職": "新任職員／初級職員", "selectable": True},
    "Lv2": {"経験年数": "1〜3年未満", "想定役職": "実務定着期職員／一般職員", "selectable": True},
    "Lv3": {"経験年数": "3年以上5年未満", "想定役職": "サブリーダー／中堅職員", "selectable": True},
    "Lv4": {"経験年数": "5年以上", "想定役職": "主任／チーフ", "selectable": False},
    "Lv5": {"経験年数": "7年以上", "想定役職": "副管理者／リーダー候補", "selectable": False},
    "Lv6": {"経験年数": "10年以上", "想定役職": "管理者／管理責任者", "selectable": False},
    "Lv7": {"経験年数": "10年以上", "想定役職": "統括管理者／拠点長", "selectable": False},
}

# =========================================================
# 認証
# =========================================================
def check_password():
    if not APP_PASSWORD:
        return True

    if "auth" not in st.session_state:
        st.session_state["auth"] = False
        st.session_state["USER_PLAN"] = "standard"

    if not st.session_state["auth"]:
        st.markdown("## 🔒 アクセス制限")
        plan_options = {
            "standard_demo": "Standard (デモ用)",
            "advanced_demo": "Advanced (デモ用)",
            "premium_demo": "Premium (デモ用)",
        }
        selected_id = st.selectbox("デモユーザーID", list(plan_options.keys()), format_func=lambda x: plan_options[x])
        st.session_state["USER_PLAN_SELECTION"] = selected_id.split("_")[0]
        pwd = st.text_input("パスワード", type="password")
        if st.button("ログイン"):
            if pwd == APP_PASSWORD:
                st.session_state["auth"] = True
                st.session_state["USER_PLAN"] = st.session_state["USER_PLAN_SELECTION"]
                st.rerun()
            else:
                st.error("パスワードが違います")
        st.stop()

    global USER_PLAN
    USER_PLAN = st.session_state["USER_PLAN"]
    return True


# =========================================================
# URL分析
# =========================================================
@st.cache_data(ttl=60 * 60, show_spinner=False)
def analyze_url_logic(url: str) -> str:
    try:
        headers = {"User-Agent": "HR-Eval-MVP/1.0"}
        resp = requests.get(url, headers=headers, timeout=12)
        if resp.status_code != 200:
            return ""
        resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "html.parser")
        for n in soup(["script", "style"]):
            n.decompose()
        candidates = []
        keywords = ["理念", "ミッション", "ビジョン", "Mission", "Vision", "Value", "指針", "社是"]
        for tag in ["h1", "h2", "h3", "div", "p"]:
            for el in soup.find_all(tag):
                text = el.get_text(strip=True)
                if any(k in text for k in keywords) and 2 <= len(text) <= 80:
                    parent = el.find_parent()
                    if parent:
                        block = parent.get_text(strip=True, separator="\n")
                        if 30 <= len(block) <= 900:
                            candidates.append(block)
        uniq = list(dict.fromkeys(candidates))
        if uniq:
            return "\n\n".join(uniq[:3])
        if soup.title and soup.title.string:
            return f"【タイトル】{soup.title.string}"
        return ""
    except Exception:
        return ""


# =========================================================
# ヘルパー関数群
# =========================================================
def get_major_categories():
    return DEFAULT_MAJOR_CATEGORIES


def default_weight_by_level(level: str) -> int:
    if level == "Lv1":
        return 1
    if level == "Lv2":
        return 2
    return 3


def normalize_weight(w, level: str) -> int:
    try:
        wi = int(w)
    except Exception:
        wi = default_weight_by_level(level)
    if wi < 1:
        wi = 1
    if wi > 5:
        wi = 5
    return wi


def to_display_df(items, major_names_map: dict):
    def sort_key(item):
        k = item.get("category_large_key")
        if k in ROMAN_ORDER:
            return ROMAN_ORDER.index(k)
        return 999

    sorted_items = sorted(items, key=sort_key)
    rows = []
    for it in sorted_items:
        key = (it.get("category_large_key") or "").strip()
        name = (it.get("category_large_name") or "").strip()
        if not name and key in major_names_map:
            name = major_names_map[key]
        major_disp = f"{key}. {name}" if key and name else (name or key or "")
        rows.append(
            {
                "大分類": major_disp,
                "中分類": (it.get("category_medium") or "").strip(),
                "設問": (it.get("full_sentence") or "").strip(),
                "ウエイト": int(it.get("weight", 0)) if str(it.get("weight", "")).strip() != "" else 0,
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        df = pd.DataFrame(columns=["大分類", "中分類", "設問", "ウエイト"])
    df.insert(0, "NO", range(1, len(df) + 1))
    return df[["NO", "大分類", "中分類", "設問", "ウエイト"]]


def to_excel(items, major_names_map: dict, meta: dict):
    df = to_display_df(items, major_names_map)
    meta_df = pd.DataFrame(
        [
            {"項目": "会社名", "値": meta.get("company_name", "")},
            {"項目": "企業URL", "値": meta.get("company_url", "")},
            {"項目": "事業所名", "値": meta.get("office_name", "")},
            {"項目": "職種", "値": meta.get("role", "")},
            {"項目": "レベル", "値": meta.get("level", "")},
            {"項目": "経験年数", "値": meta.get("level_years", "")},
            {"項目": "想定役職", "値": meta.get("level_role", "")},
            {"項目": "適用年度", "値": meta.get("generation_year", "")},
            {"項目": "設問数", "値": str(meta.get("count", ""))},
            {"項目": "生成日", "値": meta.get("generated_at", "")},
            {"項目": "プラン", "値": meta.get("plan", "")},
            {"項目": "環境", "値": meta.get("env", "")},
        ]
    )
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        meta_df.to_excel(writer, index=False, sheet_name="メタ情報")
        df.to_excel(writer, index=False, sheet_name="評価シート")
    return output.getvalue()


def allocate_counts(total: int, keys: list, weights: dict) -> dict:
    n = len(keys)
    alloc = {k: 0 for k in keys}
    if total <= 0 or n == 0:
        return alloc

    if total < n:
        sorted_keys = sorted(keys, key=lambda k: float(weights.get(k, 1.0)), reverse=True)
        for k in sorted_keys[:total]:
            alloc[k] = 1
        return alloc

    wsum = sum(float(weights.get(k, 1.0)) for k in keys)
    if wsum <= 0:
        wsum = float(n)

    target = {k: round(total * float(weights.get(k, 1.0)) / wsum) for k in keys}
    for k in keys:
        if target[k] < 1:
            target[k] = 1

    diff = total - sum(target.values())
    sorted_keys = sorted(keys, key=lambda k: float(weights.get(k, 1.0)), reverse=True)
    i = 0
    while diff != 0 and i < 10000:
        k = sorted_keys[i % n]
        if diff > 0:
            target[k] += 1
            diff -= 1
        else:
            if target[k] > 1:
                target[k] -= 1
                diff += 1
        i += 1

    diff2 = total - sum(target.values())
    if diff2 != 0:
        target[sorted_keys[0]] += diff2

    return target


# =========================================================
# AI生成ロジック
# =========================================================
def _hash_text(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:16]


@st.cache_data(ttl=60 * 60, show_spinner=False)
def cached_generate_mediums(cache_key: str, payload: dict) -> list:
    _ = cache_key
    return generate_mediums(**payload)


def generate_mediums(role, level, major_key, major_name, philosophy, values, ng, grow, philosophy_rate, company_name):
    if not client:
        return []

    sys = f"""
IMPORTANT: Output strictly in JSON format. (json)

あなたは9人の経営理論(ドラッカー/野中/カプラン等)を体得した人事コンサルタントです。

【会社】{company_name}
【職種】{role}
【レベル】{level}
【大分類】{major_key}. {major_name}
【理念】{philosophy}

中分類(評価観点)を5〜7個作成せよ。
Output JSON: {{ "mediums": [ {{ "name": "...", "intent": "...", "weight": 1.0 }} ] }}
""".strip()

    try:
        res = client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": sys}],
            temperature=0.4,
        )
        mediums = json.loads(res.choices[0].message.content).get("mediums", [])
        return mediums if isinstance(mediums, list) else []
    except Exception:
        return []


def call_model_for_questions(
    role,
    level,
    mk,
    mname,
    med,
    intent,
    count,
    phi,
    val,
    ng,
    gr,
    exist,
    rate,
    year,
    comp,
):
    if not client:
        return []

    existing_list = "\n".join([f"- {q}" for q in (exist or [])][-80:])

    sys = f"""
IMPORTANT: Output strictly in JSON format. (json)

あなたは9人の経営理論を体得した人事コンサルタントです。
「{mk}. {mname} > {med}」の設問を作成します。

【ルール】
- 文頭に職種名は入れない
- 「目的＋行動＋成果」を1文に統合する
- 文末は「〜している」
- 要素分解(purpose, action, result)も出力する
- 既存設問との重複・類似は禁止（言い換えも不可）
- 出力件数は必ず {count} 件

【既存設問（重複禁止）】
{existing_list}

Output JSON:
{{
  "items": [
    {{
      "purpose": "...",
      "action": "...",
      "result": "...",
      "full_sentence": "...",
      "weight": 3
    }}
  ]
}}
""".strip()

    try:
        res = client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": sys}],
            temperature=0.6,
        )
        items = json.loads(res.choices[0].message.content).get("items", [])
        return items if isinstance(items, list) else []
    except Exception:
        return []


def check_duplicates(items, threshold=0.75):
    duplicates = []
    n = len(items)
    for i in range(n):
        for j in range(i + 1, n):
            s1 = (items[i].get("full_sentence") or "").strip()
            s2 = (items[j].get("full_sentence") or "").strip()
            if not s1 or not s2:
                continue
            score = difflib.SequenceMatcher(None, s1, s2).ratio()
            if score >= threshold:
                k1 = items[i].get("category_large_key")
                k2 = items[j].get("category_large_key")
                dup_type = "group" if k1 == k2 else "global"
                duplicates.append((i, items[i], j, items[j], score, dup_type))
    return duplicates


def regenerate_specific_item(
    item,
    role,
    level,
    philosophy,
    values,
    ng,
    grow,
    philosophy_rate,
    generation_year,
    company_name,
    mode="duplicate",
    existing_questions=None,
):
    if not client:
        return item

    instruction = (
        "他の設問と重複しないように、全く異なる切り口で書き直してください。"
        if mode == "duplicate"
        else "同じ中分類内で、評価の視点を変えて（別の観察ポイントで）書き直してください。"
    )

    existing_list = "\n".join([f"- {q}" for q in (existing_questions or [])][-80:])

    sys = f"""
IMPORTANT: Output strictly in JSON format. (json)

あなたは人事評価制度の専門家です。
{instruction}

【元の設問】
{item.get('full_sentence','')}

【大分類】
{item.get('category_large_name','')}

【中分類】
{item.get('category_medium','')}

【既存設問（重複禁止）】
{existing_list}

Output JSON:
{{
  "purpose": "...",
  "action": "...",
  "result": "...",
  "full_sentence": "...",
  "weight": 3
}}
""".strip()

    try:
        res = client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": sys}],
            temperature=0.7,
        )
        new_item = item.copy()
        new_item.update(json.loads(res.choices[0].message.content))
        return new_item
    except Exception:
        return item


def generate_items(role, level, total, phi, val, ng, gr, weights, names, rate, year, comp, thresh):
    major_keys = sorted([k for k in weights.keys() if k in ROMAN_ORDER], key=lambda x: ROMAN_ORDER.index(x))
    alloc = allocate_counts(total, major_keys, weights)

    final = []
    seen_q = set()
    total_steps = max(1, len(major_keys) * 6)
    step = 0
    all_mediums = {}

    bar = st.progress(0)
    status = st.empty()

    def _set_progress(step_now: int, cap: float) -> None:
        # 0〜100 の int に統一（環境依存の不具合を避ける）
        p = min(step_now / total_steps, cap)
        bar.progress(int(p * 100))

    for mk in major_keys:
        need = int(alloc.get(mk, 0))
        if need <= 0:
            continue

        mname = names.get(mk, mk)
        step += 1
        _set_progress(step, 0.95)
        status.text(f"中分類生成: {mk}. {mname}")

        payload = {
            "role": role,
            "level": level,
            "major_key": mk,
            "major_name": mname,
            "philosophy": phi,
            "values": val,
            "ng": ng,
            "grow": gr,
            "philosophy_rate": rate,
            "company_name": comp,
        }
        key = f"{comp}|{role}|{level}|{mk}|{mname}|{_hash_text(phi)}|{_hash_text(val)}|{_hash_text(ng)}|{_hash_text(gr)}|{rate}"
        meds = cached_generate_mediums(key, payload)
        if not meds:
            meds = [{"name": "基本", "intent": "基本", "weight": 1.0}]
        all_mediums[mk] = meds

        med_alloc = allocate_counts(
            need,
            [m.get("name", "") for m in meds if m.get("name")],
            {m.get("name", ""): float(m.get("weight", 1.0)) for m in meds if m.get("name")},
        )

        for m in meds:
            med_name = (m.get("name") or "").strip()
            med_intent = (m.get("intent") or "").strip()
            n_med = int(med_alloc.get(med_name, 0))
            if not med_name or n_med <= 0:
                continue

            rounds = 0
            while n_med > 0 and rounds < 3:
                rounds += 1
                step += 1
                _set_progress(step, 0.99)
                status.text(f"生成中: {mk}. {mname} > {med_name}（残り{n_med}）")

                existing_questions = [x.get("full_sentence", "") for x in final if x.get("full_sentence")]
                got = call_model_for_questions(
                    role,
                    level,
                    mk,
                    mname,
                    med_name,
                    med_intent,
                    n_med,
                    phi,
                    val,
                    ng,
                    gr,
                    existing_questions,  # ★重複回避のため渡す
                    rate,
                    year,
                    comp,
                )

                added = 0
                for it in got:
                    q = (it.get("full_sentence") or "").strip()
                    if not q or q in seen_q:
                        continue
                    seen_q.add(q)
                    final.append(
                        {
                            "category_large_key": mk,
                            "category_large_name": mname,
                            "category_medium": med_name,
                            "full_sentence": q,
                            "purpose": it.get("purpose", ""),
                            "action": it.get("action", ""),
                            "result": it.get("result", ""),
                            "weight": normalize_weight(it.get("weight"), level),
                        }
                    )
                    added += 1

                n_med -= added
                if added == 0:
                    break
                time.sleep(0.05)

            if len(final) >= total:
                break

        if len(final) >= total:
            break

    bar.progress(100)
    status.success("生成完了")
    time.sleep(0.2)
    bar.empty()
    status.empty()

    st.session_state["mediums_debug"] = all_mediums
    st.session_state["duplicates_found"] = check_duplicates(final, threshold=thresh)
    return final[:total]


# =========================================================
# Main
# =========================================================
env_guard_or_stop()
check_password()

st.title(APP_TITLE)
st.caption(f"Version {APP_VERSION}｜Plan: {USER_PLAN}｜ENV: {ENV_LABEL}（{APP_ENV}）")

defaults = {
    "company_name": "",
    "company_url": "",
    "company_philosophy_text": "",
    "office_philosophy_text": "",
    "office_name": "",
    "role": "介護職",
    "values_text": "",
    "ng_text": "",
    "grow_text": "",
    "items": [],
    "philosophy_used": "",
    "mediums_debug": {},
    "duplicates_found": [],
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

majors = get_major_categories()
default_weights = {k: 1.0 for k in majors.keys()}
if "IV" in default_weights:
    default_weights["IV"] = 1.5

st.sidebar.markdown("## ⚙️ 設定")
if USER_PLAN == "premium":
    philosophy_rate = st.sidebar.slider("理念出現率", 0, 100, 30, 5)
    similarity_threshold = st.sidebar.slider("類似許容度", 0.5, 0.95, 0.75, 0.05)
elif USER_PLAN == "advanced":
    philosophy_rate = 30
    similarity_threshold = 0.75
    st.sidebar.caption("Advanced: 理念30% / 類似度0.75 固定")
else:
    philosophy_rate = 5
    similarity_threshold = 0.8
    st.sidebar.caption("Standard: 理念5% / 類似度0.80 固定")

selected_major_weights = default_weights.copy()
selected_major_names = majors.copy()

if USER_PLAN in ["advanced", "premium"]:
    st.sidebar.markdown("#### 💎 大分類設定")
    if "custom_weights" not in st.session_state:
        st.session_state["custom_weights"] = default_weights.copy()
    current_weights = st.session_state["custom_weights"]

    edited_weights, edited_names = {}, {}
    for k in ROMAN_ORDER:
        if k not in majors:
            continue
        c1, c2 = st.sidebar.columns([0.2, 0.8])
        is_checked = c1.checkbox(k, value=True, key=f"check_{k}")
        user_name = c2.text_input(
            "Name",
            value=majors[k],
            key=f"name_{k}",
            disabled=not is_checked,
            label_visibility="collapsed",
        )
        if is_checked:
            edited_names[k] = user_name
            w = current_weights.get(k, default_weights.get(k, 1.0))
            edited_weights[k] = st.sidebar.slider(
                "重み",
                0.5,
                3.0,
                float(w),
                0.1,
                key=f"weight_{k}",
                label_visibility="collapsed",
            )

    if not edited_weights:
        st.sidebar.warning("1つ以上選択してください")
        selected_major_weights, selected_major_names = default_weights, majors
    else:
        st.session_state["custom_weights"] = edited_weights
        selected_major_weights, selected_major_names = edited_weights, edited_names
else:
    selected_major_weights, selected_major_names = default_weights, majors

# Input Form
st.markdown("### 1. 企業情報")
company_locked = bool(FIXED_COMPANY_NAME or FIXED_COMPANY_URL)
company_name = st.text_input(
    "企業名",
    value=FIXED_COMPANY_NAME or st.session_state["company_name"],
    disabled=company_locked,
)
company_url = st.text_input(
    "企業URL",
    value=FIXED_COMPANY_URL or st.session_state["company_url"],
    disabled=company_locked,
)
if not company_locked:
    st.session_state["company_name"], st.session_state["company_url"] = company_name, company_url

effective_company_name = (FIXED_COMPANY_NAME or company_name).strip()
effective_company_url = (FIXED_COMPANY_URL or company_url).strip()

st.markdown("#### 1.1 理念コンテキスト")
st.session_state["company_philosophy_text"] = st.text_area(
    "【会社全体】理念・ミッション",
    st.session_state["company_philosophy_text"],
    height=100,
)
st.session_state["office_philosophy_text"] = st.text_area(
    "【事業所固有】現場の行動指針",
    st.session_state["office_philosophy_text"],
    height=80,
)

st.markdown("### 1.5 事業所情報")
st.session_state["office_name"] = st.text_input("事業所名", value=st.session_state["office_name"])

st.markdown("### 2. 職種")
st.session_state["role"] = st.text_input("職種", value=st.session_state["role"])

st.markdown("### 3. 評価レベル")
selectable_map = {f"{k}｜{v['経験年数']}｜{v['想定役職']}": k for k, v in USER_DEFINED_LEVELS.items() if v["selectable"]}
level_label = st.selectbox("レベル選択", list(selectable_map.keys()), index=2)
level = selectable_map[level_label]

st.markdown("### 4. 追加コンテキスト")
if USER_PLAN in ["advanced", "premium"]:
    st.session_state["values_text"] = st.text_area("価値観・社是", st.session_state["values_text"], height=80)
    st.session_state["ng_text"] = st.text_area("禁止事項", st.session_state["ng_text"], height=80)
    st.session_state["grow_text"] = st.text_area("伸ばしたい行動", st.session_state["grow_text"], height=80)
else:
    st.caption("※ アドバンス以上で利用可能")

st.markdown("### 5. 生成設定")
c_y, c_c = st.columns([1, 2])
year = c_y.text_input("適用年度", value=str(datetime.now().year + 1))
count = c_c.slider("設問数", 10, 100, 40)

if st.button("評価シート生成（AI）", type="primary"):
    if not client:
        st.error("APIキー未設定")
        st.stop()
    if not effective_company_name or not st.session_state["office_name"] or not st.session_state["role"]:
        st.error("必須項目を入力してください")
        st.stop()

    with st.spinner("AIコンサルタントが思考中..."):
        phi_gen = (st.session_state["company_philosophy_text"] or "").strip()
        if not phi_gen:
            if effective_company_url:
                url_phi = analyze_url_logic(effective_company_url).strip()
                phi_gen = url_phi if url_phi else FALLBACK_PHILOSOPHY
            else:
                phi_gen = FALLBACK_PHILOSOPHY

        combined_phi = f"【会社全体】\n{phi_gen}\n\n【事業所固有】\n{(st.session_state['office_philosophy_text'] or '').strip()}"

        items = generate_items(
            st.session_state["role"].strip(),
            level,
            count,
            combined_phi,
            st.session_state["values_text"].strip(),
            st.session_state["ng_text"].strip(),
            st.session_state["grow_text"].strip(),
            selected_major_weights,
            selected_major_names,
            philosophy_rate,
            year.strip(),
            effective_company_name,
            similarity_threshold,
        )
        st.session_state["items"] = items
        st.session_state["philosophy_used"] = combined_phi

# 結果画面
if st.session_state["items"]:
    st.markdown("---")
    st.subheader("📊 生成結果プレビュー & 品質管理")

    # 品質管理パネル
    st.markdown('<div class="control-panel">', unsafe_allow_html=True)
    st.markdown("<div class='panel-header'>🛠️ 品質管理パネル (Quality Control)</div>", unsafe_allow_html=True)

    dups = st.session_state.get("duplicates_found", [])
    group_dups = [d for d in dups if d[5] == "group"]
    global_dups = [d for d in dups if d[5] == "global"]

    c1, c2 = st.columns(2)

    # 1. グループ内重複
    with c1:
        st.markdown("**1. グループ内重複** (同一大分類)")
        if group_dups:
            st.markdown(f"<div class='alert-box-warning'>⚠️ {len(group_dups)}件の重複を検知</div>", unsafe_allow_html=True)
            for d in group_dups[:3]:
                st.caption(f"No.{d[0]+1} ≒ No.{d[2]+1}（類似度: {d[4]:.2f}）")

            if st.button("🔄 グループ内重複を解消", key="fix_grp", type="primary"):
                with st.spinner("修正中..."):
                    items = st.session_state["items"]
                    for d in group_dups:
                        idx = d[2]
                        existing_questions = [x.get("full_sentence", "") for x in items if x.get("full_sentence")]
                        items[idx] = regenerate_specific_item(
                            items[idx],
                            st.session_state["role"],
                            level,
                            st.session_state["philosophy_used"],
                            st.session_state["values_text"],
                            st.session_state["ng_text"],
                            st.session_state["grow_text"],
                            philosophy_rate,
                            year,
                            effective_company_name,
                            mode="duplicate",
                            existing_questions=existing_questions,
                        )
                    st.session_state["items"] = items
                    st.session_state["duplicates_found"] = check_duplicates(items, similarity_threshold)
                    st.success("修正完了")
                    time.sleep(0.6)
                    st.rerun()
        else:
            st.markdown("<div class='alert-box-success'>✅ 重複なし</div>", unsafe_allow_html=True)

    # 2. 全体重複
    with c2:
        st.markdown("**2. 全体重複** (大分類またぎ)")
        if global_dups:
            st.markdown(f"<div class='alert-box-warning'>⚠️ {len(global_dups)}件の重複を検知</div>", unsafe_allow_html=True)
            for d in global_dups[:3]:
                st.caption(f"No.{d[0]+1} ≒ No.{d[2]+1}（類似度: {d[4]:.2f}）")

            if st.button("🔄 全体重複を解消", key="fix_glb", type="primary"):
                with st.spinner("修正中..."):
                    items = st.session_state["items"]
                    for d in global_dups:
                        idx = d[2]
                        existing_questions = [x.get("full_sentence", "") for x in items if x.get("full_sentence")]
                        items[idx] = regenerate_specific_item(
                            items[idx],
                            st.session_state["role"],
                            level,
                            st.session_state["philosophy_used"],
                            st.session_state["values_text"],
                            st.session_state["ng_text"],
                            st.session_state["grow_text"],
                            philosophy_rate,
                            year,
                            effective_company_name,
                            mode="duplicate",
                            existing_questions=existing_questions,
                        )
                    st.session_state["items"] = items
                    st.session_state["duplicates_found"] = check_duplicates(items, similarity_threshold)
                    st.success("修正完了")
                    time.sleep(0.6)
                    st.rerun()
        else:
            st.markdown("<div class='alert-box-success'>✅ 重複なし</div>", unsafe_allow_html=True)

    st.markdown("---")

    # 3. 視点変更
    st.markdown("**3. 視点変更 (個別ブラッシュアップ)**")
    c_sel, c_btn = st.columns([0.3, 0.7])
    target_no = c_sel.number_input("修正対象No", 1, len(st.session_state["items"]), 1)
    if c_btn.button(f"🔄 No.{target_no} を別の視点で書き直す", key="fix_ind"):
        with st.spinner("書き直し中..."):
            idx = int(target_no) - 1
            items = st.session_state["items"]
            existing_questions = [x.get("full_sentence", "") for x in items if x.get("full_sentence")]
            items[idx] = regenerate_specific_item(
                items[idx],
                st.session_state["role"],
                level,
                st.session_state["philosophy_used"],
                st.session_state["values_text"],
                st.session_state["ng_text"],
                st.session_state["grow_text"],
                philosophy_rate,
                year,
                effective_company_name,
                mode="perspective",
                existing_questions=existing_questions,
            )
            st.session_state["items"] = items
            st.session_state["duplicates_found"] = check_duplicates(items, similarity_threshold)
            st.success("完了")
            time.sleep(0.6)
            st.rerun()

    st.markdown("</div>", unsafe_allow_html=True)

    # テーブル表示
    df = to_display_df(st.session_state["items"], selected_major_names)
    st.dataframe(df, hide_index=True, use_container_width=True)

    # ダウンロード
    meta = {
        "company_name": effective_company_name,
        "company_url": effective_company_url,
        "office_name": st.session_state["office_name"],
        "role": st.session_state["role"],
        "level": level,
        "level_years": USER_DEFINED_LEVELS[level]["経験年数"],
        "level_role": USER_DEFINED_LEVELS[level]["想定役職"],
        "generation_year": year,
        "count": count,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "plan": USER_PLAN,
        "env": APP_ENV,
    }
    excel = to_excel(st.session_state["items"], selected_major_names, meta)

    c_dl, c_sv = st.columns([2, 1])
    c_dl.download_button(
        "⬇ Excelダウンロード",
        excel,
        f"評価シート_{st.session_state['office_name']}.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="secondary",
    )

    # “物理ガード”として保存導線を封印できる（WRITE_ENABLED=false ならボタン出さない）
    if WRITE_ENABLED:
        c_sv.button("💾 クラウド保存 (次回)")
    else:
        c_sv.caption("💾 クラウド保存：WRITE_ENABLED=false（封印中）")

    if st.session_state["philosophy_used"]:
        with st.expander("使用した理念"):
            st.text(st.session_state["philosophy_used"])

    if USER_PLAN in ["advanced", "premium"] and st.session_state["mediums_debug"]:
        with st.expander("思考プロセス (中分類)"):
            for k, v in st.session_state["mediums_debug"].items():
                st.markdown(f"**{k}. {selected_major_names.get(k, k)}**")
                for m in v:
                    st.markdown(f"- {m.get('name','')}: {m.get('intent','')}")
