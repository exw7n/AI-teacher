import os
import json
import time
import re
import base64
import hashlib
import hmac
import sqlite3
import secrets
from pathlib import Path
from datetime import datetime

import streamlit as st
from dotenv import load_dotenv
from streamlit_autorefresh import st_autorefresh
from gigachat import GigaChat
from gigachat.models import ChatCompletionRequest, ChatMessage

load_dotenv()

APP_NAME = "ИИ-преподаватель"
APP_VERSION = "1.0.1"
AUTHOR = "Ваше имя"
WEAK_FILE = Path("weak_questions.json")
AUTH_DB = Path("users.sqlite3")
MAX_IMAGE_SIZE = 15 * 1024 * 1024

DEFAULT_SETTINGS = {
    "question_count": 5,
    "difficulty": "Средний",
    "time_limit": 60,  # 0 = без ограничения
    "language": "Русский",
    "repeat_enabled": True,
    "repeat_threshold": 2,
    "theme": "Светлая",
}


def auth_db():
    """Create/migrate account table so older deployed SQLite schemas won't crash."""
    with sqlite3.connect(AUTH_DB) as conn:
        conn.row_factory = sqlite3.Row
        existing = conn.execute("PRAGMA table_info(users)").fetchall()
        columns = {row["name"] for row in existing}
        required = {"id", "full_name", "username", "email", "password_hash", "created_at"}
        if not existing:
            conn.execute("""CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""")
        elif not required.issubset(columns):
            # Rebuild the old table while preserving accounts where possible.
            conn.execute("ALTER TABLE users RENAME TO users_legacy_migration")
            conn.execute("""CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""")
            legacy_rows = conn.execute("SELECT * FROM users_legacy_migration").fetchall()
            legacy_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users_legacy_migration)").fetchall()}
            for row in legacy_rows:
                username = str(row["username"] if "username" in legacy_columns else row["login"] if "login" in legacy_columns else "").strip()
                if not username:
                    continue
                full_name = str(row["full_name"] if "full_name" in legacy_columns and row["full_name"] else username)
                email = str(row["email"] if "email" in legacy_columns and row["email"] else f"{username}@legacy.invalid").strip().lower()
                stored = row["password_hash"] if "password_hash" in legacy_columns else row["password"] if "password" in legacy_columns else ""
                if not stored:
                    continue
                # Older versions sometimes stored raw passwords. Hash them on migration.
                if ":" not in str(stored):
                    stored = hash_password(str(stored))
                created = str(row["created_at"] if "created_at" in legacy_columns and row["created_at"] else datetime.now().isoformat(timespec="seconds"))
                try:
                    conn.execute("INSERT INTO users(full_name, username, email, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                                (full_name, username, email, stored, created))
                except sqlite3.IntegrityError:
                    continue
            conn.execute("DROP TABLE users_legacy_migration")

def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
    return salt.hex() + ":" + digest.hex()

def verify_password(password, stored_hash):
    try:
        salt_hex, digest_hex = stored_hash.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False

def render_auth():
    auth_db()
    if "auth_mode" not in st.session_state:
        st.session_state.auth_mode = "login"

    mode = st.session_state.auth_mode
    st.markdown(f"""<div class="auth-brand">
      <div class="auth-brand-title">{APP_NAME}</div>
      <div class="auth-brand-subtitle">Твой личный помощник в учёбе</div>
      <div class="auth-brand-accent"></div>
    </div>""", unsafe_allow_html=True)

    _, center, _ = st.columns([1, 1.45, 1])
    with center:
        with st.container(border=True, key="auth_shell"):
            if mode == "login":
                st.markdown("### С возвращением ✦")
                st.caption("Войди, чтобы продолжить учёбу")
                with st.form("login_form", clear_on_submit=False):
                    login = st.text_input("Логин или email", placeholder="Введите логин или email")
                    password = st.text_input("Пароль", type="password", placeholder="Введите пароль")
                    submitted = st.form_submit_button("Войти", use_container_width=True)
                if submitted:
                    if not login.strip() or not password:
                        st.error("Заполните логин и пароль.")
                    else:
                        with sqlite3.connect(AUTH_DB) as conn:
                            user = conn.execute(
                                "SELECT id, full_name, username, email, password_hash FROM users WHERE username=? OR email=? COLLATE NOCASE",
                                (login.strip(), login.strip())
                            ).fetchone()
                        if user and verify_password(password, user[4]):
                            st.session_state.auth_user = {"id": user[0], "full_name": user[1], "username": user[2], "email": user[3]}
                            st.rerun()
                        else:
                            st.error("Неверный логин/email или пароль.")
                st.markdown("<div style='text-align:center;color:#7b8aa4;margin:.65rem 0'>Нет аккаунта?</div>", unsafe_allow_html=True)
                if st.button("Зарегистрироваться", use_container_width=True, key="show_register"):
                    st.session_state.auth_mode = "register"
                    st.rerun()
            else:
                st.markdown("### Создать аккаунт ✦")
                st.caption("Заполни данные — это займёт всего минуту")
                with st.form("register_form", clear_on_submit=True):
                    full_name = st.text_input("Имя", placeholder="Как к тебе обращаться")
                    username = st.text_input("Логин", placeholder="3–24 символа, латиница и цифры")
                    email = st.text_input("Email", placeholder="name@example.com")
                    new_password = st.text_input("Пароль", type="password", placeholder="Минимум 8 символов")
                    confirm = st.text_input("Повтори пароль", type="password", placeholder="Повтори пароль")
                    register = st.form_submit_button("Создать аккаунт", use_container_width=True)
                if register:
                    full_name, username, email = full_name.strip(), username.strip(), email.strip().lower()
                    if not all([full_name, username, email, new_password, confirm]):
                        st.error("Заполни все поля регистрации.")
                    elif len(username) < 3 or len(username) > 24 or not re.fullmatch(r"[A-Za-z0-9_.-]+", username):
                        st.error("Логин должен быть 3–24 символа: латинские буквы, цифры, _ . или -.")
                    elif not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
                        st.error("Укажи корректный email.")
                    elif len(new_password) < 8:
                        st.error("Пароль должен быть не короче 8 символов.")
                    elif new_password != confirm:
                        st.error("Пароли не совпадают.")
                    else:
                        try:
                            with sqlite3.connect(AUTH_DB) as conn:
                                conn.execute("INSERT INTO users(full_name, username, email, password_hash, created_at) VALUES (?, ?, ?, ?, ?)", (full_name, username, email, hash_password(new_password), datetime.now().isoformat(timespec="seconds")))
                            st.success("Аккаунт создан! Теперь войди.")
                            st.session_state.auth_mode = "login"
                            st.rerun()
                        except sqlite3.IntegrityError:
                            st.error("Такой логин или email уже зарегистрирован. Выбери другой.")
                if st.button("← Вернуться ко входу", use_container_width=True, key="back_to_login"):
                    st.session_state.auth_mode = "login"
                    st.rerun()
    st.markdown("<div style='text-align:center;margin-top:1rem;color:#8391a7;font-size:.8rem'>Ваши данные аккаунта хранятся в базе приложения.</div>", unsafe_allow_html=True)

def load_weak_questions():
    if not WEAK_FILE.exists():
        return []
    try:
        with WEAK_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_weak_questions():
    temp_path = WEAK_FILE.with_suffix(".tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as f:
            json.dump(st.session_state.weak_data, f, ensure_ascii=False, indent=2)
        temp_path.replace(WEAK_FILE)
    except OSError as exc:
        st.error(f"Не удалось сохранить weak_questions.json: {exc}")


def init_state():
    defaults = {
        "settings": DEFAULT_SETTINGS.copy(),
        "quiz_questions": [],
        "quiz_index": 0,
        "quiz_correct": 0,
        "quiz_wrong": 0,
        "quiz_finished": False,
        "quiz_active": False,
        "quiz_review_mode": False,
        "quiz_deadline": None,
        "quiz_feedback": None,
        "quiz_history": [],
        "weak_data": None,
        "clear_weak_confirm": False,
        "clear_history_confirm": False,
    }
    if "auth_user" not in st.session_state:
        st.session_state.auth_user = None
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value
    if st.session_state.weak_data is None:
        st.session_state.weak_data = load_weak_questions()


def get_gigachat():
    client_id = os.getenv("GIGACHAT_CLIENT_ID")
    client_secret = os.getenv("GIGACHAT_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "Задайте GIGACHAT_CLIENT_ID и GIGACHAT_CLIENT_SECRET "
            "в переменных окружения или в файле .env."
        )

    credentials = base64.b64encode(
        f"{client_id}:{client_secret}".encode("utf-8")
    ).decode("utf-8")

    # Имя модели можно переопределить через GIGACHAT_MODEL.
    model = os.getenv("GIGACHAT_MODEL", "GigaChat-2")
    return GigaChat(
        credentials=credentials,
        scope=os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
        model=model,
        verify_ssl_certs=True,
    )


def response_text(response):
    """Совместимость с ответами разных версий SDK."""
    if getattr(response, "choices", None):
        message = response.choices[0].message
        content = getattr(message, "content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks = []
            for part in content:
                text = getattr(part, "text", None)
                if text:
                    chunks.append(text)
                elif isinstance(part, dict) and part.get("text"):
                    chunks.append(part["text"])
            return "\n".join(chunks)

    if getattr(response, "messages", None):
        message = response.messages[0]
        content = getattr(message, "content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks = []
            for part in content:
                text = getattr(part, "text", None)
                if text:
                    chunks.append(text)
            return "\n".join(chunks)

    raise RuntimeError("GigaChat вернул пустой ответ.")


def ask_gigachat(prompt, image_bytes=None, image_name="note.png"):
    with get_gigachat() as client:
        attachments = None

        if image_bytes is not None:
            # Актуальный SDK принимает файл как (имя, байты).
            uploaded = client.upload_file((image_name, image_bytes), purpose="general")
            file_id = getattr(uploaded, "id_", None) or getattr(uploaded, "id", None)
            if not file_id:
                raise RuntimeError("GigaChat не вернул ID загруженного изображения.")
            attachments = [file_id]

        message = ChatMessage(
            role="user",
            content=prompt,
            attachments=attachments,
        )
        request = ChatCompletionRequest(
            model=os.getenv("GIGACHAT_MODEL", "GigaChat-2"),
            messages=[message],
            temperature=0.2,
        )
        return response_text(client.chat.create(request))


def parse_json_response(raw_text):
    text = raw_text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"(\[[\s\S]*\]|\{[\s\S]*\})", text)
        if not match:
            raise ValueError("Модель не вернула корректный JSON.")
        return json.loads(match.group(1))


def recognize_notes(image_bytes, image_name):
    return ask_gigachat(
        "Прочитай изображение конспекта. Распознай печатный и рукописный "
        "текст, формулы, заголовки и списки. Не придумывай отсутствующие "
        "фрагменты; неразборчивые места обозначь как [неразборчиво]. "
        "Верни только распознанный текст.",
        image_bytes=image_bytes,
        image_name=image_name,
    )


def generate_questions(notes, settings):
    prompt = f"""
Создай ровно {settings['question_count']} вопросов по конспекту.
Сложность: {settings['difficulty']}.
Язык: {settings['language']}.
Вопросы должны проверять содержание конспекта, не повторяться и иметь
конкретный эталонный ответ и краткое объяснение.
Верни только JSON-массив:
[
  {{
    "question": "Вопрос",
    "answer": "Эталонный ответ",
    "explanation": "Объяснение"
  }}
]
Конспект:
{notes}
"""
    data = parse_json_response(ask_gigachat(prompt))
    if not isinstance(data, list):
        raise ValueError("Ожидался JSON-массив вопросов.")
    questions = []
    for item in data:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question", "")).strip()
        answer = str(item.get("answer", "")).strip()
        explanation = str(item.get("explanation", "")).strip()
        if question and answer:
            questions.append({
                "question": question,
                "answer": answer,
                "explanation": explanation or "Объяснение не предоставлено.",
            })
    if not questions:
        raise ValueError("Не удалось создать вопросы по конспекту.")
    return questions[: settings["question_count"]]


def evaluate_answer(question, expected, user_answer, language):
    prompt = f"""
Проверь ответ ученика, оценивая смысл, а не дословное совпадение.
Вопрос: {question}
Эталонный ответ: {expected}
Ответ ученика: {user_answer}
Язык: {language}
Пустой или не относящийся к вопросу ответ — неправильный.
Верни только JSON:
{{"correct": true, "explanation": "Краткое объяснение"}}
"""
    result = parse_json_response(ask_gigachat(prompt))
    if not isinstance(result, dict) or "correct" not in result:
        raise ValueError("Модель вернула некорректный результат проверки.")
    return bool(result["correct"]), str(result.get("explanation", ""))


def add_weak_question(question, answer, explanation):
    for item in st.session_state.weak_data:
        if item.get("question", "").strip().casefold() == question.strip().casefold():
            item["correct_answer"] = answer
            item["explanation"] = explanation
            item.setdefault("correct_streak", 0)
            item.setdefault("total_attempts", 0)
            item.setdefault("correct_attempts", 0)
            item.setdefault("wrong_attempts", 0)
            item["last_seen"] = datetime.now().isoformat(timespec="seconds")
            save_weak_questions()
            return
    st.session_state.weak_data.append({
        "id": f"{int(time.time() * 1000)}-{len(st.session_state.weak_data)}",
        "question": question,
        "correct_answer": answer,
        "explanation": explanation,
        "correct_streak": 0,
        "total_attempts": 0,
        "correct_attempts": 0,
        "wrong_attempts": 0,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "last_seen": datetime.now().isoformat(timespec="seconds"),
    })
    save_weak_questions()


def update_weak_attempt(question, is_correct):
    for item in list(st.session_state.weak_data):
        if item.get("question") != question:
            continue
        item["total_attempts"] = item.get("total_attempts", 0) + 1
        item["last_seen"] = datetime.now().isoformat(timespec="seconds")
        if is_correct:
            item["correct_attempts"] = item.get("correct_attempts", 0) + 1
            item["correct_streak"] = item.get("correct_streak", 0) + 1
        else:
            item["wrong_attempts"] = item.get("wrong_attempts", 0) + 1
            item["correct_streak"] = 0

        if item["correct_streak"] >= st.session_state.settings["repeat_threshold"]:
            st.session_state.weak_data.remove(item)
            save_weak_questions()
            return True
        save_weak_questions()
        return False
    return False


def start_quiz(questions, review_mode=False):
    st.session_state.quiz_questions = list(questions)
    st.session_state.quiz_index = 0
    st.session_state.quiz_correct = 0
    st.session_state.quiz_wrong = 0
    st.session_state.quiz_finished = False
    st.session_state.quiz_active = bool(questions)
    st.session_state.quiz_review_mode = review_mode
    st.session_state.quiz_feedback = None
    limit = st.session_state.settings["time_limit"]
    st.session_state.quiz_deadline = time.time() + limit if questions and limit else None


def finish_quiz():
    st.session_state.quiz_active = False
    st.session_state.quiz_finished = True
    st.session_state.quiz_deadline = None
    st.session_state.quiz_history.append({
        "date": datetime.now().isoformat(timespec="seconds"),
        "correct": st.session_state.quiz_correct,
        "wrong": st.session_state.quiz_wrong,
        "total": len(st.session_state.quiz_questions),
        "review_mode": st.session_state.quiz_review_mode,
    })


def submit_answer(user_answer="", timed_out=False):
    idx = st.session_state.quiz_index
    if idx >= len(st.session_state.quiz_questions):
        return
    item = st.session_state.quiz_questions[idx]
    question = item["question"]
    expected = item["answer"]
    explanation = item.get("explanation", "")

    if timed_out:
        is_correct = False
        feedback = "Время вышло. " + explanation
    else:
        try:
            is_correct, feedback = evaluate_answer(
                question, expected, user_answer,
                st.session_state.settings["language"]
            )
        except Exception as exc:
            st.error(f"Не удалось проверить ответ: {exc}")
            return

    if is_correct:
        st.session_state.quiz_correct += 1
    else:
        st.session_state.quiz_wrong += 1
        if not st.session_state.quiz_review_mode:
            add_weak_question(question, expected, explanation)

    removed = False
    if st.session_state.quiz_review_mode:
        removed = update_weak_attempt(question, is_correct)
        if removed:
            feedback += "\nВопрос удалён из слабых мест: порог достигнут."

    st.session_state.quiz_feedback = {
        "correct": is_correct,
        "explanation": feedback,
        "correct_answer": expected,
        "timed_out": timed_out,
    }
    st.session_state.quiz_deadline = None


def next_question():
    st.session_state.quiz_index += 1
    st.session_state.quiz_feedback = None
    if st.session_state.quiz_index >= len(st.session_state.quiz_questions):
        finish_quiz()
        return
    limit = st.session_state.settings["time_limit"]
    st.session_state.quiz_deadline = time.time() + limit if limit else None


def render_quiz():
    questions = st.session_state.quiz_questions
    idx = st.session_state.quiz_index
    if not questions:
        st.info("Нет вопросов для прохождения.")
        return
    if idx >= len(questions):
        finish_quiz()
        st.rerun()

    item = questions[idx]
    st.progress((idx + 1) / len(questions))
    st.caption(f"Вопрос {idx + 1} из {len(questions)}")
    st.subheader(item["question"])

    deadline = st.session_state.quiz_deadline
    if deadline is not None and st.session_state.quiz_feedback is None:
        remaining = max(0, int(deadline - time.time() + 0.999))
        if remaining <= 0:
            submit_answer(timed_out=True)
            st.rerun()
        st.metric("Осталось времени", f"{remaining} сек.")
        st_autorefresh(interval=1000, limit=0, key=f"timer_{idx}_{int(deadline)}")

    feedback = st.session_state.quiz_feedback
    if feedback is None:
        with st.form(key=f"answer_form_{idx}_{st.session_state.quiz_review_mode}"):
            answer = st.text_input("Ваш ответ", key=f"answer_input_{idx}_{st.session_state.quiz_review_mode}")
            submitted = st.form_submit_button("Ответить")
        if submitted:
            if not answer.strip():
                st.warning("Введите ответ перед отправкой.")
            else:
                submit_answer(answer.strip())
                st.rerun()
    else:
        if feedback["correct"]:
            st.success("✅ Правильно!")
        else:
            st.error("❌ Неправильно.")
        if feedback["timed_out"]:
            st.warning("Время на ответ закончилось.")
        st.markdown("**Правильный ответ:**")
        st.write(feedback["correct_answer"])
        st.markdown("**Объяснение:**")
        st.write(feedback["explanation"])
        if st.button("Следующий вопрос", type="primary"):
            next_question()
            st.rerun()


def render_results():
    st.subheader("Результаты")
    correct = st.session_state.quiz_correct
    wrong = st.session_state.quiz_wrong
    total = correct + wrong
    a, b, c = st.columns(3)
    a.metric("Всего", total)
    b.metric("Правильных", correct)
    c.metric("Неправильных", wrong)
    if total:
        st.progress(correct / total)
        st.write(f"Результат: **{correct / total:.0%}**")
    if st.button("Начать заново", type="primary"):
        st.session_state.quiz_active = False
        st.session_state.quiz_finished = False
        st.session_state.quiz_questions = []
        st.session_state.quiz_feedback = None
        st.session_state.quiz_deadline = None
        st.rerun()


def page_test():
    st.markdown(
        """
        <div style="padding:1.45rem 1.55rem; border-radius:22px; margin-bottom:1.25rem;
                    background:linear-gradient(125deg,#2864e8 0%,#4389f5 58%,#66c8f2 100%);
                    box-shadow:0 14px 30px rgba(40,100,232,.18); color:white;">
            <div style="font-size:.76rem; font-weight:750; letter-spacing:.13em;
                        text-transform:uppercase; opacity:.86; margin-bottom:.55rem;">
                YOUR PERSONAL AI TUTOR
            </div>
            <div style="font-size:2rem; line-height:1.12; font-weight:850; letter-spacing:-.045em;
                        margin-bottom:.55rem; color:white;">
                Учись в своём ритме ✦
            </div>
            <div style="font-size:1rem; line-height:1.55; max-width:38rem; color:white; opacity:.92;">
                Загрузи конспект — ИИ превратит его в персональный тест,
                поможет разобраться в ошибках и закрепить знания.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown("### 📝 Создать новый тест")
    st.write("Добавь фото конспекта, чтобы подготовить вопросы для тренировки.")
    if not st.session_state.quiz_active and not st.session_state.quiz_finished:
        upload = st.file_uploader("Фото конспекта", type=["jpg", "jpeg", "png"])
        if upload:
            if upload.size > MAX_IMAGE_SIZE:
                st.error("Файл больше 15 МБ.")
                return
            st.image(upload, caption="Загруженный конспект", use_container_width=True)
            if st.button("Распознать и создать тест", type="primary"):
                try:
                    with st.spinner("Распознаю текст..."):
                        notes = recognize_notes(upload.getvalue(), upload.name)
                    if not notes.strip():
                        st.error("Текст на изображении не распознан.")
                        return
                    with st.expander("Распознанный текст"):
                        st.write(notes)
                    with st.spinner("Создаю вопросы..."):
                        questions = generate_questions(notes, st.session_state.settings)
                    start_quiz(questions)
                    st.rerun()
                except Exception as exc:
                    st.error(f"Не удалось создать тест: {exc}")
    if st.session_state.quiz_active and not st.session_state.quiz_review_mode:
        render_quiz()
    if st.session_state.quiz_finished and not st.session_state.quiz_review_mode:
        render_results()


def page_weak():
    st.title("🎯 Твоя зона роста")
    st.caption("Собираем ошибки в одном месте, чтобы превращать их в уверенные знания.")
    weak = st.session_state.weak_data
    threshold = st.session_state.settings["repeat_threshold"]
    st.write(f"Вопросов для повторения: **{len(weak)}**")
    st.caption(f"Порог удаления: {threshold} правильных ответов подряд.")
    if not st.session_state.settings["repeat_enabled"]:
        st.info("Повторение отключено в настройках. Список доступен для просмотра.")
    if weak:
        for i, item in enumerate(weak, start=1):
            with st.expander(f"{i}. {item.get('question', 'Вопрос')}"):
                st.markdown("**Правильный ответ:**")
                st.write(item.get("correct_answer", ""))
                st.markdown("**Объяснение:**")
                st.write(item.get("explanation", ""))
                st.caption(
                    f"Правильных подряд: {item.get('correct_streak', 0)} / {threshold}; "
                    f"попыток: {item.get('total_attempts', 0)}"
                )
    else:
        st.success("Слабых мест пока нет.")

    if st.session_state.quiz_active and st.session_state.quiz_review_mode:
        render_quiz()
    elif st.session_state.quiz_finished and st.session_state.quiz_review_mode:
        render_results()
    elif weak and st.session_state.settings["repeat_enabled"]:
        if st.button("Пройти повторно", type="primary"):
            start_quiz(st.session_state.weak_data.copy(), review_mode=True)
            st.rerun()


def page_settings():
    st.title("⚙️ Настройки")
    s = st.session_state.settings
    st.subheader("Параметры теста")
    s["question_count"] = st.slider("Количество вопросов", 3, 10, s["question_count"])
    s["difficulty"] = st.radio("Уровень сложности", ["Лёгкий", "Средний", "Сложный"],
                               index=["Лёгкий", "Средний", "Сложный"].index(s["difficulty"]))
    options = {"30 секунд": 30, "60 секунд": 60, "90 секунд": 90, "Без ограничения": 0}
    current = next((label for label, value in options.items() if value == s["time_limit"]), "60 секунд")
    selected = st.selectbox("Время на вопрос", list(options), index=list(options).index(current))
    s["time_limit"] = options[selected]
    s["language"] = st.radio("Язык вопросов", ["Русский", "English"],
                             index=["Русский", "English"].index(s["language"]))

    st.divider()
    st.subheader("Повторение слабых мест")
    s["repeat_enabled"] = st.toggle("Включить повторение", value=s["repeat_enabled"])
    s["repeat_threshold"] = st.slider("Порог повторения", 1, 3, s["repeat_threshold"])
    if st.button("Очистить слабые места"):
        st.session_state.clear_weak_confirm = True
    if st.session_state.clear_weak_confirm:
        st.warning("Удалить все слабые места?")
        col1, col2 = st.columns(2)
        if col1.button("Да, очистить", key="yes_clear_weak"):
            st.session_state.weak_data = []
            save_weak_questions()
            st.session_state.clear_weak_confirm = False
            st.rerun()
        if col2.button("Отмена", key="no_clear_weak"):
            st.session_state.clear_weak_confirm = False
            st.rerun()

    st.divider()
    st.subheader("Внешний вид")
    s["theme"] = st.radio("Тема оформления", ["Светлая", "Тёмная"],
                          index=["Светлая", "Тёмная"].index(s["theme"]))
    st.caption("Тема сохраняется в настройках приложения. Цвета интерфейса Streamlit задаются отдельно.")

    st.divider()
    st.subheader("Данные")
    export = {
        "app": APP_NAME,
        "version": APP_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "settings": s,
        "history": st.session_state.quiz_history,
        "weak_questions": st.session_state.weak_data,
    }
    st.download_button("Скачать мои результаты (JSON)",
                       data=json.dumps(export, ensure_ascii=False, indent=2),
                       file_name="ai_teacher_results.json", mime="application/json")
    if st.button("Очистить историю"):
        st.session_state.clear_history_confirm = True
    if st.session_state.clear_history_confirm:
        st.warning("Удалить всю историю результатов?")
        col1, col2 = st.columns(2)
        if col1.button("Да, удалить", key="yes_clear_history"):
            st.session_state.quiz_history = []
            st.session_state.clear_history_confirm = False
            st.rerun()
        if col2.button("Отмена", key="no_clear_history"):
            st.session_state.clear_history_confirm = False
            st.rerun()

    st.divider()
    st.subheader("О приложении")
    st.write(f"**Название:** {APP_NAME}")
    st.write(f"**Версия:** {APP_VERSION}")
    st.write(f"**Автор:** {AUTHOR}")
    st.write("**Технологии:** Python, Streamlit, GigaChat API, JSON")



def apply_theme():
    """Polished, responsive interface with a blue accent and light/dark themes."""
    theme = st.session_state.settings.get("theme", "Светлая")
    if theme == "Тёмная":
        palette = """
        :root {
            color-scheme: dark;
            --app-bg: #0b1020;
            --app-glow: rgba(59,130,246,.12);
            --panel-bg: #121a2d;
            --panel-soft: #172238;
            --panel-border: #273650;
            --text-main: #edf3ff;
            --text-muted: #9aacc7;
            --accent: #4f8cff;
            --accent-hover: #76a7ff;
            --shadow: 0 12px 32px rgba(0,0,0,.20);
        }
        [data-testid="stHeader"] { background: rgba(11,16,32,.88); }
        [data-testid="stSidebar"] { background: #0e1628; }
        """
    else:
        palette = """
        :root {
            color-scheme: light;
            --app-bg: #f6f8fd;
            --app-glow: rgba(73,139,255,.10);
            --panel-bg: #ffffff;
            --panel-soft: #f0f5ff;
            --panel-border: #e0e8f5;
            --text-main: #18243b;
            --text-muted: #6b7b95;
            --accent: #2864e8;
            --accent-hover: #174fc9;
            --shadow: 0 12px 32px rgba(32,65,120,.07);
        }
        [data-testid="stHeader"] { background: rgba(246,248,253,.88); }
        [data-testid="stSidebar"] { background: #ffffff; }
        """

    css = """
    <style>
    """ + palette + """
    html, body, [class*="css"] { font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    .stApp, [data-testid="stAppViewContainer"] {
        background:
            radial-gradient(ellipse at 85% 0%, var(--app-glow), transparent 32rem),
            var(--app-bg);
        color: var(--text-main);
    }
    [data-testid="stMain"] > div { padding-top: 2.2rem; }
    [data-testid="stSidebar"] {
        border-right: 1px solid var(--panel-border);
        box-shadow: 8px 0 28px rgba(25,55,100,.025);
    }
    [data-testid="stSidebar"] > div { padding: 1.4rem 1rem; }
    [data-testid="stSidebar"] * { color: var(--text-main); }
    [data-testid="stSidebar"] h1 {
        font-size: 1.18rem;
        font-weight: 800;
        letter-spacing: -.035em;
        padding: .6rem .4rem 1rem;
    }
    h1, h2, h3 {
        color: var(--text-main);
        letter-spacing: -.04em;
        font-weight: 750;
    }
    h1 { font-size: clamp(1.8rem, 3vw, 2.45rem); line-height: 1.15; margin-bottom: .45rem; }
    h2 { font-size: 1.5rem; }
    h3 { font-size: 1.15rem; }
    p, label, .stMarkdown, [data-testid="stMetricValue"] { color: var(--text-main); }
    [data-testid="stCaptionContainer"], .stCaption, small { color: var(--text-muted) !important; }
    [data-testid="stVerticalBlock"] { gap: 1rem; }
    div[data-testid="stMetric"], div[data-testid="stExpander"], div[data-testid="stForm"],
    div[data-testid="stAlert"], div[data-testid="stFileUploader"],
    div[data-testid="stImage"] {
        border: 1px solid var(--panel-border);
        border-radius: 18px;
        background: var(--panel-bg);
        box-shadow: var(--shadow);
        overflow: hidden;
    }
    div[data-testid="stMetric"] { padding: 1rem 1.1rem; }
    [data-testid="stMetricLabel"] { color: var(--text-muted) !important; font-weight: 600; }
    [data-testid="stMetricValue"] { font-weight: 800; letter-spacing: -.04em; }
    div[data-testid="stExpander"] { padding: .15rem .3rem; }
    div[data-testid="stForm"] { padding: 1rem; }
    div[data-baseweb="input"] > div, div[data-baseweb="textarea"] > div,
    div[data-baseweb="select"] > div {
        background: var(--panel-bg);
        border: 1px solid var(--panel-border);
        border-radius: 12px;
        min-height: 2.8rem;
    }
    input, textarea { color: var(--text-main) !important; }
    [data-baseweb="select"] * { color: var(--text-main) !important; }
    div.stButton > button, div.stDownloadButton > button,
    div[data-testid="stFormSubmitButton"] > button {
        background: linear-gradient(135deg, var(--accent), #4b8dff);
        color: #fff !important;
        border: 0;
        border-radius: 12px;
        min-height: 2.8rem;
        padding: .55rem 1.15rem;
        font-weight: 700;
        box-shadow: 0 7px 16px rgba(40,100,232,.18);
        transition: transform .16s ease, box-shadow .16s ease, filter .16s ease;
    }
    div.stButton > button:hover, div.stDownloadButton > button:hover,
    div[data-testid="stFormSubmitButton"] > button:hover {
        filter: brightness(1.06);
        transform: translateY(-1px);
        box-shadow: 0 10px 22px rgba(40,100,232,.24);
        border: 0;
        color: #fff !important;
    }
    div.stButton > button:focus, div.stDownloadButton > button:focus {
        box-shadow: 0 0 0 3px rgba(75,141,255,.25);
    }
    [data-testid="stProgressBar"] > div {
        background: var(--panel-border);
        border-radius: 999px;
        height: .55rem;
    }
    [data-testid="stProgressBar"] > div > div {
        background: linear-gradient(90deg, #55c5f7, var(--accent));
        border-radius: 999px;
    }
    [data-testid="stFileUploader"] {
        background: var(--panel-bg);
        border: 1.5px dashed #8bb5f7;
        padding: .4rem;
    }
    [data-testid="stFileUploader"] section {
        background: var(--panel-soft);
        border: 0;
        border-radius: 13px;
    }
    [data-testid="stRadio"] > div { gap: .5rem; }
    [data-testid="stRadio"] label {
        border-radius: 10px;
        padding: .35rem .55rem;
    }
    [data-testid="stSlider"] [role="slider"] { border-color: var(--accent); }
    hr { border-color: var(--panel-border); opacity: .8; margin: 1.6rem 0; }
    [data-testid="stStatusWidget"] { border-radius: 12px; }
    /* Auth screen uses the same blue-cyan accent as the main app */
    .auth-brand { max-width:700px; margin:2.3rem auto 1.1rem; text-align:center; }
    .auth-brand-title { font-size:2.2rem; font-weight:850; letter-spacing:-.045em; color:var(--text-main); }
    .auth-brand-subtitle { margin-top:.45rem; color:var(--text-muted); font-size:.98rem; }
    .auth-brand-accent { margin:1.15rem auto 0; width:58px; height:5px; border-radius:99px; background:linear-gradient(90deg,#2864e8,#66c8f2); }
    .st-key-auth_shell { background:var(--panel-bg) !important; border:1px solid var(--panel-border) !important; border-radius:22px !important; padding:1.25rem !important; box-shadow:var(--shadow) !important; }
    .st-key-auth_shell input, .st-key-auth_shell [data-baseweb="input"] > div, .st-key-auth_shell [data-baseweb="input"] { background:var(--panel-soft) !important; color:var(--text-main) !important; border:1px solid var(--panel-border) !important; border-radius:11px !important; box-shadow:none !important; }
    .st-key-auth_shell input, .st-key-auth_shell input::placeholder { -webkit-text-fill-color:var(--text-main) !important; color:var(--text-main) !important; }
    .st-key-auth_shell input::placeholder { opacity:.58 !important; }
    .st-key-auth_shell label, .st-key-auth_shell p, .st-key-auth_shell h3, .st-key-auth_shell [data-testid="stMarkdownContainer"] { color:var(--text-main) !important; }
    .st-key-auth_shell button { border-radius:12px !important; }
    .st-key-auth_shell [data-testid="stForm"] { background:transparent !important; border:0 !important; box-shadow:none !important; padding:0 !important; }
    .st-key-auth_shell [data-testid="stAlert"] { background:var(--panel-soft) !important; color:var(--text-main) !important; border:1px solid var(--panel-border) !important; }
    @media (max-width: 700px) {
        [data-testid="stMain"] > div { padding: 1.1rem .8rem 2rem; }
        h1 { font-size: 1.8rem; }
    }
    </style>
    """
    st.markdown(css, unsafe_allow_html=True)

def main():
    st.set_page_config(page_title=APP_NAME, page_icon="📘", layout="wide", initial_sidebar_state="expanded")
    init_state()
    apply_theme()
    if not st.session_state.get("auth_user"):
        render_auth()
        return
    # Account-specific weak-points storage and reset temporary quiz state on account switch.
    user = st.session_state.auth_user
    owner_id = str(user["id"])
    if st.session_state.get("_data_owner") != owner_id:
        global WEAK_FILE
        WEAK_FILE = Path(f"weak_questions_user_{owner_id}.json")
        st.session_state.weak_data = load_weak_questions()
        st.session_state.quiz_history = []
        st.session_state.quiz_active = False
        st.session_state.quiz_finished = False
        st.session_state.quiz_questions = []
        st.session_state.quiz_feedback = None
        st.session_state._data_owner = owner_id
    st.sidebar.markdown(
        """
        <div style="padding:.6rem .55rem 1rem;">
            <div style="width:44px;height:44px;border-radius:14px;
                        display:flex;align-items:center;justify-content:center;
                        background:linear-gradient(135deg,#2864e8,#66c8f2);
                        color:white;font-size:1.35rem;margin-bottom:.8rem;
                        box-shadow:0 8px 18px rgba(40,100,232,.22);">✦</div>
            <div style="font-size:1.08rem;font-weight:850;letter-spacing:-.035em;">AI Study Space</div>
            <div style="font-size:.78rem;opacity:.65;margin-top:.2rem;">Твой уютный уголок знаний</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    user = st.session_state.auth_user
    st.sidebar.caption(f"👤 {user['full_name']}")
    if st.sidebar.button("Выйти из аккаунта", use_container_width=True):
        st.session_state.auth_user = None
        st.rerun()
    page = st.sidebar.radio("МЕНЮ", ["Тест", "Слабые места", "Настройки"])
    st.sidebar.markdown(
        f'<div style="margin:1.2rem .55rem;padding:.7rem .8rem;border-radius:12px;'
        f'background:rgba(100,150,220,.09);font-size:.75rem;opacity:.75;">'
        f'✧ Версия {APP_VERSION}<br>Создано для твоего прогресса</div>',
        unsafe_allow_html=True,
    )

    if page == "Тест":
        page_test()
    elif page == "Слабые места":
        page_weak()
    else:
        page_settings()


if __name__ == "__main__":
    main()
