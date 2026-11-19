import os
import json
import time
import re
import base64
from pathlib import Path
from datetime import datetime
import io
import html

import streamlit as st
from dotenv import load_dotenv
from streamlit_autorefresh import st_autorefresh
from langchain_gigachat import GigaChat as LC_GigaChat
from langchain_core.messages import HumanMessage

import fitz  # PyMuPDF

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

APP_NAME = "ИИ-преподаватель"
APP_VERSION = "20.1.0"
AUTHOR = "Марина Левченко"
MAX_IMAGE_SIZE = 15 * 1024 * 1024

DEFAULT_SETTINGS = {
    "question_count": 5,
    "difficulty": "Средний",
    "language": "Русский",
    "repeat_enabled": True,
    "repeat_threshold": 2,
    "theme": "Тёмная",
}


def get_user_file(suffix):
    username = st.session_state.get("username", "guest")
    return Path(f"{suffix}_{username}.json")


def load_json_file(path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, (list, dict)) else default
    except (OSError, json.JSONDecodeError):
        return default


def save_json_file(path, data):
    temp_path = path.with_suffix(".tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        temp_path.replace(path)
    except OSError as exc:
        st.error(f"Не удалось сохранить {path.name}: {exc}")


def init_state():
    defaults = {
        "settings": DEFAULT_SETTINGS.copy(),
        "quiz_history": [],
        "weak_data": [],
        "current_page": "test",
        "quiz_active": False,
        "current_question_idx": 0,
        "questions_list": [],
        "user_answers": [],
        "answered": False,
        "selected_option": [],
        "history_saved_for_this_quiz": False,
        "selected_options_for_current_q": [],
        "logged_in": False,
        "username": "",
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def get_lc_gigachat():
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

    return LC_GigaChat(
        base_url="https://api.giga.chat/v1",
        credentials=credentials,
        scope=os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
        model="GigaChat-2-Max",
        verify_ssl_certs=False,
        timeout=600,
    )


def ask_gigachat_lc(prompt, image_bytes=None):
    llm = get_lc_gigachat()

    if image_bytes is None:
        response = llm.invoke(prompt)
        return response.content

    uploaded = llm.upload_file(("note.png", image_bytes))

    message = HumanMessage(
        content_blocks=[
            {"type": "text", "text": prompt},
            {"type": "image", "file_id": uploaded.id_},
        ]
    )

    response = llm.invoke([message])
    return response.content


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
    prompt = (
        "Прочитай текст на изображении конспекта. "
        "Верни ТОЛЬКО распознанный текст без своих комментариев и без markdown-разметки. "
        "Если текст не виден, напиши ровно одно слово: ERROR_NO_TEXT"
    )
    result = ask_gigachat_lc(prompt, image_bytes=image_bytes)

    result = result.replace("\\", "/")

    if "ERROR_NO_TEXT" in result or len(result.strip()) < 30:
        raise ValueError(f"GigaChat не смог распознать текст. Ответ модели: '{result[:100]}...'")

    return result


def generate_questions(notes, settings):
    if len(notes.strip()) < 50 or "ERROR_NO_TEXT" in notes:
        raise ValueError("Текст конспекта слишком короткий или не распознан.")

    prompt = f"""
    Ты — строгий учитель. Вот текст конспекта:
    {notes}

    Создай ровно {settings['question_count']} вопросов СТРОГО по этому тексту.
    Сложность: {settings['difficulty']}. Язык: {settings['language']}.

    Для каждого вопроса предложи 4 варианта ответа. Вопросы могут иметь ОДИН или НЕСКОЛЬКО правильных ответов.
    Верни ТОЛЬКО JSON-массив:
    [
      {{
        "question": "Текст вопроса",
        "options": ["Вариант 1", "Вариант 2", "Вариант 3", "Вариант 4"],
        "correct_answers": ["Точный текст правильного варианта 1", "Точный текст правильного варианта 2 (если есть)"],
        "explanation": "Краткое объяснение"
      }}
    ]
    """

    data = parse_json_response(ask_gigachat_lc(prompt))
    questions = []
    for i in data:
        if i.get("question") and i.get("options"):
            questions.append({
                "question": str(i.get("question")),
                "options": [str(o) for o in i.get("options", [])],
                "correct_answers": [str(a) for a in i.get("correct_answers", [])],
                "explanation": str(i.get("explanation", ""))
            })
    return questions[:settings["question_count"]]


def add_weak_question(question, answer, explanation):
    for item in st.session_state.weak_data:
        if item.get("question", "").strip().casefold() == question.strip().casefold():
            item["correct_answer"] = answer
            item["explanation"] = explanation
            item["last_seen"] = datetime.now().isoformat(timespec="seconds")
            save_json_file(get_user_file("weak"), st.session_state.weak_data)
            return
    st.session_state.weak_data.append({
        "id": f"{int(time.time() * 1000)}",
        "question": question, "correct_answer": answer, "explanation": explanation,
        "correct_streak": 0, "total_attempts": 0,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "last_seen": datetime.now().isoformat(timespec="seconds"),
    })
    save_json_file(get_user_file("weak"), st.session_state.weak_data)


def page_test():
    history = st.session_state.quiz_history
    total_quizzes = len(history)
    total_correct = sum(h["correct"] for h in history)
    total_total = sum(h["total"] for h in history)
    accuracy = (total_correct / total_total * 100) if total_total else 0
    weak_count = len(st.session_state.weak_data)

    if st.session_state.quiz_active:
        q_idx = st.session_state.current_question_idx
        questions = st.session_state.questions_list

        if q_idx >= len(questions):
            correct_count = sum(1 for ans in st.session_state.user_answers if ans["is_correct"])
            partial_count = sum(1 for ans in st.session_state.user_answers if ans.get("is_partial"))
            total_count = len(questions)

            if not st.session_state.get("history_saved_for_this_quiz", False):
                st.session_state.quiz_history.append({
                    "date": datetime.now().isoformat(timespec="seconds"),
                    "correct": correct_count,
                    "wrong": total_count - correct_count,
                    "total": total_count
                })
                st.session_state.history_saved_for_this_quiz = True
                save_json_file(get_user_file("history"), st.session_state.quiz_history)

            st.caption("ТЕСТ ЗАВЕРШЕН")
            st.title("Отличная работа!")
            
            st.markdown(f"""
            <div style="background: var(--glass-bg); border: 1px solid var(--glass-border); border-radius: 16px; padding: 1.5rem; margin-bottom: 2rem;">
                <div style="color: var(--text-muted); font-size: 0.9rem; margin-bottom: 0.5rem;">Результат</div>
                <div style="font-size: 2rem; font-weight: 700; color: var(--text-main);">{correct_count} / {total_count} правильных</div>
                <div style="font-size: 1rem; color: var(--accent); margin-top: 0.5rem;">{partial_count} частично правильных</div>
            </div>
            """, unsafe_allow_html=True)

            if st.button("Вернуться на главную", type="primary"):
                st.session_state.quiz_active = False
                st.session_state.questions_list = []
                st.session_state.user_answers = []
                st.session_state.current_question_idx = 0
                st.session_state.history_saved_for_this_quiz = False
                st.session_state.selected_options_for_current_q = []
                st.rerun()
            return

        current_q = questions[q_idx]

        col1, col2 = st.columns([1, 4])
        with col1:
            st.markdown(f'<div class="question-badge">Вопрос {q_idx + 1}</div>', unsafe_allow_html=True)
        with col2:
            progress_val = (q_idx + 1) / len(questions) * 100
            st.markdown(
                f'<div class="quiz-progress-container">'
                f'<div class="quiz-progress-track" role="progressbar">'
                f'<div class="quiz-progress-fill" style="width: {progress_val:.2f}%"></div>'
                f'</div>'
                f'<div class="quiz-progress-text">{q_idx + 1} / {len(questions)}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

        st.markdown(
            f'<div class="quiz-question-title">{html.escape(str(current_q["question"]))}</div>',
            unsafe_allow_html=True,
        )

        if f"q_init_{q_idx}" not in st.session_state:
            st.session_state.selected_options_for_current_q = []
            st.session_state[f"q_init_{q_idx}"] = True

        if not st.session_state.answered:
            for option in current_q["options"]:
                is_selected = option in st.session_state.selected_options_for_current_q
                
                if st.button(option, key=f"opt_btn_{q_idx}_{option}", use_container_width=True, type="primary" if is_selected else "secondary"):
                    if is_selected:
                        st.session_state.selected_options_for_current_q.remove(option)
                    else:
                        st.session_state.selected_options_for_current_q.append(option)
                    st.rerun()

            if st.button("Ответить", type="primary", use_container_width=True, disabled=(len(st.session_state.selected_options_for_current_q) == 0)):
                selected = st.session_state.selected_options_for_current_q
                correct_set = set(current_q["correct_answers"])
                selected_set = set(selected)
                
                is_correct = (selected_set == correct_set)
                is_partial = bool(selected_set.intersection(correct_set)) and not is_correct

                st.session_state.user_answers.append({
                    "question": current_q["question"],
                    "user_choice": selected,
                    "correct_answer": current_q["correct_answers"],
                    "is_correct": is_correct,
                    "is_partial": is_partial,
                })

                if not is_correct:
                    add_weak_question(current_q["question"], ", ".join(current_q["correct_answers"]), current_q["explanation"])

                st.session_state.answered = True
                st.rerun()
        else:
            last_answer = st.session_state.user_answers[-1]

            if last_answer["is_correct"]:
                st.markdown("""
                <div style="background: rgba(34, 197, 94, 0.15); border: 1px solid #22c55e; border-radius: 12px; padding: 1rem; color: #22c55e; font-weight: 600; margin-bottom: 1rem;">
                    Верно!
                </div>
                """, unsafe_allow_html=True)
            elif last_answer.get("is_partial"):
                st.markdown("""
                <div style="background: rgba(245, 158, 11, 0.15); border: 1px solid #f59e0b; border-radius: 12px; padding: 1rem; color: #f59e0b; font-weight: 600; margin-bottom: 1rem;">
                    Частично верно. Вы выбрали не все правильные варианты.
                </div>
                """, unsafe_allow_html=True)
                st.markdown(f"**Правильный ответ:** {', '.join(last_answer['correct_answer'])}")
            else:
                st.markdown("""
                <div style="background: rgba(239, 68, 68, 0.15); border: 1px solid #ef4444; border-radius: 12px; padding: 1rem; color: #ef4444; font-weight: 600; margin-bottom: 1rem;">
                    Неверно.
                </div>
                """, unsafe_allow_html=True)
                st.markdown(f"**Правильный ответ:** {', '.join(last_answer['correct_answer'])}")

            st.markdown(f"""
            <div style="background: rgba(99, 140, 255, 0.1); border: 1px solid rgba(99, 140, 255, 0.2); border-radius: 12px; padding: 1rem; color: var(--text-main); margin-top: 1rem;">
                <b>Объяснение:</b> {current_q['explanation']}
            </div>
            """, unsafe_allow_html=True)

            if st.button("Следующий вопрос", type="primary"):
                st.session_state.current_question_idx += 1
                st.session_state.answered = False
                st.session_state.selected_options_for_current_q = []
                st.rerun()

        return

    st.caption("ТВОЯ АКТИВНОСТЬ")
    st.title("Мой учебный день")

    st.markdown(f"""
    <div class="stats-grid" style="margin-bottom: 2rem;">
        <div class="stats-card">
            <div class="stats-top"><span>Пройдено тестов</span>
                <div class="icon-box">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>
                </div>
            </div>
            <div class="stats-value">{total_quizzes}</div>
            <div class="stats-sub">всего завершено</div>
        </div>
        <div class="stats-card">
            <div class="stats-top"><span>Точность ответов</span>
                <div class="icon-box">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                </div>
            </div>
            <div class="stats-value">{accuracy:.0f}<span class="percent">%</span></div>
            <div class="stats-sub">правильных ответов</div>
        </div>
        <div class="stats-card">
            <div class="stats-top"><span>Слабые места</span>
                <div class="icon-box">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/></svg>
                </div>
            </div>
            <div class="stats-value">{weak_count}</div>
            <div class="stats-sub">темы для повторения</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    col_left, col_right = st.columns([2, 1], gap="large")

    with col_left:
        st.markdown("""
        <div class="custom-card" style="padding-bottom: 0.5rem;">
            <div class="page-subtitle" style="font-size: 0.7rem;">НОВЫЙ МАТЕРИАЛ</div>
            <h2 style="margin-top: 0; margin-bottom: 1rem;">Создать тест по конспекту</h2>
            <p style="color: var(--text-muted); margin-bottom: 0.5rem;">Загрузи фото или PDF. Мы распознаем текст и подготовим вопросы.</p>
        """, unsafe_allow_html=True)

        # Кастомный загрузчик файлов
        upload = st.file_uploader("Загрузите фото или PDF", type=["jpg", "jpeg", "png", "pdf"], label_visibility="collapsed", key="file_uploader")
        
        if upload:
            if upload.size > MAX_IMAGE_SIZE:
                st.error("Файл больше 15 МБ.")
            else:
                file_bytes = upload.getvalue()
                image_bytes = None

                if upload.name.lower().endswith(".pdf"):
                    try:
                        doc = fitz.open(stream=file_bytes, filetype="pdf")
                        if len(doc) == 0:
                            st.error("PDF файл пуст.")
                        else:
                            page = doc[0]
                            pix = page.get_pixmap()
                            image_bytes = pix.tobytes("png")
                            st.image(image_bytes, caption=f"Страница 1 из {len(doc)} (PDF)", use_container_width=True)
                    except Exception as e:
                        st.error(f"Ошибка при чтении PDF: {e}")
                else:
                    image_bytes = file_bytes
                    st.image(image_bytes, use_container_width=True)

                if image_bytes:
                    if st.button("Начать обучение", type="primary", use_container_width=True):
                        with st.spinner("Распознаю текст и готовлю вопросы..."):
                            try:
                                notes = recognize_notes(image_bytes, upload.name)
                                questions = generate_questions(notes, st.session_state.settings)

                                st.session_state.questions_list = questions
                                st.session_state.current_question_idx = 0
                                st.session_state.quiz_active = True
                                st.session_state.user_answers = []
                                st.session_state.answered = False
                                st.session_state.history_saved_for_this_quiz = False
                                st.session_state.selected_options_for_current_q = []
                                st.rerun()
                            except Exception as e:
                                st.error(f"Ошибка: {e}")
        else:
            st.markdown("""
            <div style="text-align: center; color: var(--text-muted); margin-top: 1.5rem; font-size: 0.9rem;">
                ✦ Перетащите файл сюда или нажмите кнопку выше
            </div>
        </div>
            """, unsafe_allow_html=True)

    with col_right:
        st.markdown("""
        <div class="custom-card" style="margin-bottom: 1.5rem;">
            <div class="page-subtitle" style="font-size: 0.7rem;">ПРОДОЛЖИТЬ</div>
            <h3 style="margin-top: 0; margin-bottom: 0.5rem; color: var(--text-main);">Твой прогресс</h3>
            <p style="color: var(--text-muted); font-size: 0.9rem; margin-bottom: 1.5rem;">Возвращайся к материалам и закрепляй знания небольшими порциями.</p>
        """, unsafe_allow_html=True)

        if weak_count > 0:
            if st.button(f"Повторить слабые места ({weak_count})", use_container_width=True):
                st.session_state.current_page = "weak"
                st.rerun()
        else:
            st.markdown("""
            <div style="text-align: center; margin-top: 1rem;">
                <div style="font-size: 2rem; margin-bottom: 0.5rem;">✦</div>
                <div style="font-weight: 600; color: var(--text-main);">Вас ждет первый тест</div>
                <div style="font-size: 0.8rem; color: var(--text-muted); margin-top: 0.5rem;">Загрузите конспект — и здесь появится история обучения.</div>
            </div>
            """, unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

        st.markdown("""
        <div class="custom-card" style="background: rgba(99, 140, 255, 0.1); border-color: rgba(99, 140, 255, 0.2);">
            <div style="display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.5rem;">
                <span style="color: var(--accent); font-size: 1.2rem;">✦</span>
                <span style="font-weight: 600; color: var(--text-main);">Совет</span>
            </div>
            <div style="font-size: 0.85rem; color: var(--text-muted); line-height: 1.5;">
                Лучше регулярно проходить короткие тесты, чем пытаться выучить все за один раз.
            </div>
        </div>
        """, unsafe_allow_html=True)


def page_weak():
    st.title("Слабые места")
    st.caption("Собираем ошибки в одном месте, чтобы превращать их в уверенные знания.")
    weak = st.session_state.weak_data
    threshold = st.session_state.settings["repeat_threshold"]
    st.write(f"Вопросов для повторения: **{len(weak)}**")
    st.caption(f"Пока не ответишь правильно {threshold} раза подряд — вопрос остаётся здесь.")

    if weak:
        for i, item in enumerate(weak, start=1):
            with st.expander(f"{i}. {item.get('question', 'Вопрос')}"):
                st.markdown("**Правильный ответ:**")
                st.write(item.get("correct_answer", ""))
                st.markdown("**Объяснение:**")
                st.write(item.get("explanation", ""))
    else:
        st.success("Слабых мест пока нет.")


def page_stats():
    st.caption("ТВОЙ ПРОГРЕСС")
    st.title("Результаты говорят сами")
    st.caption("Каждый тест — ещё один шаг к уверенному знанию.")

    history = st.session_state.quiz_history
    total_quizzes = len(history)
    total_correct = sum(h["correct"] for h in history)
    total_wrong = sum(h["wrong"] for h in history)
    total_questions = total_correct + total_wrong
    avg_percent = (total_correct / total_questions * 100) if total_questions else 0

    now = datetime.now()
    week_ago = now.timestamp() - 7 * 24 * 60 * 60
    quizzes_this_week = 0
    for h in history:
        dt = datetime.fromisoformat(h["date"])
        if dt.timestamp() >= week_ago:
            quizzes_this_week += 1

    dynamic_text = "Пока нет данных"
    if total_quizzes >= 2:
        recent = history[-10:]
        prev = history[-20:-10] if len(history) >= 20 else []

        recent_correct = sum(h["correct"] for h in recent)
        recent_total = sum(h["total"] for h in recent)
        recent_avg = (recent_correct / recent_total * 100) if recent_total else 0

        if prev:
            prev_correct = sum(h["correct"] for h in prev)
            prev_total = sum(h["total"] for h in prev)
            prev_avg = (prev_correct / prev_total * 100) if prev_total else 0
            diff = recent_avg - prev_avg
            if diff > 0:
                dynamic_text = f"↗ +{diff:.0f}% к прошлым"
            elif diff < 0:
                dynamic_text = f"↘ {diff:.0f}% к прошлым"
            else:
                dynamic_text = "Без изменений"
        else:
            dynamic_text = "Начало обучения"

    st.markdown(f"""
    <div class="stats-grid">
        <div class="stats-card">
            <div class="stats-top"><span>Пройдено тестов</span>
                <div class="icon-box">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>
                </div>
            </div>
            <div class="stats-value">{total_quizzes}</div>
            <div class="stats-sub">{f"↗ {quizzes_this_week} за эту неделю" if quizzes_this_week > 0 else "Начни свой первый тест"}</div>
        </div>
        <div class="stats-card">
            <div class="stats-top"><span>Правильных ответов</span>
                <div class="icon-box">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                </div>
            </div>
            <div class="stats-value">{total_correct}</div>
            <div class="stats-sub">{dynamic_text}</div>
        </div>
        <div class="stats-card">
            <div class="stats-top"><span>Средний балл</span>
                <div class="icon-box">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/></svg>
                </div>
            </div>
            <div class="stats-value">{avg_percent:.0f}<span class="percent">%</span></div>
            <div class="stats-sub">{'Отличный темп!' if avg_percent >= 80 else 'Хороший результат' if avg_percent >= 50 else 'Есть куда расти' if total_quizzes > 0 else 'Ждем первый тест'}</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    if history:
        st.markdown('<div class="chart-container"><h3>Динамика правильных ответов</h3></div>', unsafe_allow_html=True)
        
        recent_history = [h for h in history[-10:] if h["total"]]
        
        if recent_history:
            bars_html = '<div class="custom-chart">'
            for h in recent_history:
                percent = round(h["correct"] / h["total"] * 100)
                date_str = h["date"][:10]
                color = "var(--accent)" if percent >= 50 else "#ef4444"
                
                bars_html += f'''
                <div class="chart-bar-wrapper">
                    <div class="chart-bar-value">{percent}%</div>
                    <div class="chart-bar" style="height: {max(percent, 5)}%; background: {color};"></div>
                    <div class="chart-bar-label">{date_str[5:]}</div>
                </div>
                '''
            bars_html += '</div>'
            st.markdown(bars_html, unsafe_allow_html=True)
    else:
        st.info("Пока нет данных. Пройди первый тест!")


def page_history():
    st.caption("ТВОЯ АКТИВНОСТЬ")
    st.title("История тестов")
    st.caption("Все пройденные материалы в одном месте.")

    history = st.session_state.quiz_history
    if not history:
        st.info("История пуста. Пройди первый тест.")
        return

    for h in reversed(history):
        percent = round(h["correct"] / h["total"] * 100) if h["total"] else 0
        dt = datetime.fromisoformat(h["date"])
        date_str = dt.strftime("%d %B, %H:%M")
        color_class = "percent-good" if percent >= 80 else "percent-ok" if percent >= 50 else "percent-bad"

        st.markdown(f"""
        <div class="history-item">
            <div class="history-left">
                <div class="history-title">Тест по конспекту</div>
                <div class="history-sub">{date_str} • {h['total']} вопросов</div>
            </div>
            <div class="history-right {color_class}">{percent}%</div>
        </div>
        """, unsafe_allow_html=True)


def page_settings():
    st.title("Настройки")
    s = st.session_state.settings

    st.markdown("### Параметры теста")

    st.markdown("**Сколько вопросов создавать в одном тесте**")
    qc_options = [3, 5, 7, 10]
    cols = st.columns(len(qc_options))
    for i, opt in enumerate(qc_options):
        with cols[i]:
            is_active = (s["question_count"] == opt)
            if st.button(str(opt), key=f"qc_{opt}", use_container_width=True, type="primary" if is_active else "secondary"):
                s["question_count"] = opt
                st.rerun()

    st.markdown("<br>", unsafe_allow_html=True)

    st.markdown("**Насколько сложные вопросы**")
    diff_options = ["Лёгкий", "Средний", "Сложный"]
    cols = st.columns(len(diff_options))
    for i, opt in enumerate(diff_options):
        with cols[i]:
            is_active = (s["difficulty"] == opt)
            if st.button(opt, key=f"diff_{opt}", use_container_width=True, type="primary" if is_active else "secondary"):
                s["difficulty"] = opt
                st.rerun()

    st.markdown("<br>", unsafe_allow_html=True)

    st.markdown("**На каком языке задавать вопросы**")
    lang_options = ["Русский", "English"]
    cols = st.columns(len(lang_options))
    for i, opt in enumerate(lang_options):
        with cols[i]:
            is_active = (s["language"] == opt)
            if st.button(opt, key=f"lang_{opt}", use_container_width=True, type="primary" if is_active else "secondary"):
                s["language"] = opt
                st.rerun()

    st.markdown("### Внешний вид")

    st.markdown("**Тема оформления**")
    theme_options = ["Светлая", "Тёмная"]
    cols = st.columns(len(theme_options))
    for i, opt in enumerate(theme_options):
        with cols[i]:
            is_active = (s["theme"] == opt)
            if st.button(opt, key=f"theme_{opt}", use_container_width=True, type="primary" if is_active else "secondary"):
                s["theme"] = opt
                st.rerun()

    st.markdown("### Данные")
    export = {"app": APP_NAME, "version": APP_VERSION, "exported_at": datetime.now().isoformat(timespec="seconds"), "settings": s, "history": st.session_state.quiz_history, "weak_questions": st.session_state.weak_data}
    st.download_button("Скачать мои результаты (JSON)", data=json.dumps(export, ensure_ascii=False, indent=2), file_name="ai_teacher_results.json", mime="application/json")

    if st.button("Очистить историю"):
        st.session_state.quiz_history = []
        save_json_file(get_user_file("history"), st.session_state.quiz_history)
        st.rerun()

    st.markdown("### О приложении")
    st.write(f"**Название:** {APP_NAME}")
    st.write(f"**Версия:** {APP_VERSION}")
    st.write(f"**Автор:** {AUTHOR}")
    st.write("**Технологии:** Python, Streamlit, GigaChat API, JSON")


def page_about():
    st.title("О проекте")
    st.markdown(
        """
        **«Умный конспект»** — веб-приложение с искусственным интеллектом
        для проверки знаний по конспектам.

        **Как работает:**
        1. Загружаешь фото или PDF конспекта
        2. ИИ распознаёт текст
        3. Генерирует вопросы по материалу
        4. Ты выбираешь правильный ответ
        5. Слабые места сохраняются и повторяются

        **Технологии:** Python, Streamlit, GigaChat API, JSON.
        """
    )
    st.divider()
    st.write(f"**Версия:** {APP_VERSION}")
    st.write(f"**Автор:** {AUTHOR}")


def apply_theme():
    theme = st.session_state.settings.get("theme", "Тёмная")
    if theme == "Тёмная":
        palette = """
        :root {
            --bg-deep: #0a0f1e; --bg-mid: #0f1628; --bg-glow-1: rgba(99, 140, 255, 0.14);
            --bg-glow-2: rgba(168, 120, 255, 0.10); --glass-bg: rgba(255, 255, 255, 0.04);
            --glass-border: rgba(255, 255, 255, 0.10); --glass-hover: rgba(255, 255, 255, 0.07);
            --panel-bg: #141c30; --panel-soft: #1e2840;
            --panel-border: rgba(255, 255, 255, 0.08); --text-main: #eef2fb;
            --text-muted: #8593ad; --text-dim: #5a6680; --accent: #638cff;
            --accent-2: #a878ff; --shadow-soft: 0 20px 60px rgba(0, 0, 0, 0.35);
            --pill-bg: rgba(20, 28, 48, 0.85); --pill-text: #eef2fb; --pill-border: rgba(255, 255, 255, 0.10);
        }
        """
    else:
        palette = """
        :root {
            --bg-deep: #f0f7ff; --bg-mid: #e2edfb; --bg-glow-1: rgba(99, 140, 255, 0.14);
            --bg-glow-2: rgba(120, 200, 255, 0.12); --glass-bg: rgba(255, 255, 255, 0.85);
            --glass-border: rgba(160, 180, 220, 0.30); --glass-hover: rgba(255, 255, 255, 1);
            --panel-bg: #ffffff; --panel-soft: #f4f8ff; --panel-border: rgba(160, 180, 220, 0.35);
            --text-main: #1a2842; --text-muted: #5a6b88; --text-dim: #8fa0bc;
            --accent: #5b8def; --accent-2: #8a7bea; --shadow-soft: 0 12px 40px rgba(80, 120, 180, 0.10);
            --pill-bg: #ffffff; --pill-text: #1a2842; --pill-border: #d0dae8;
        }
        """

    css = """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Playfair+Display:wght@500;600;700&family=Montserrat:wght@500;600;700&display=swap');
    """ + palette + """
    * { transition: background-color .25s ease, border-color .25s ease, color .25s ease, transform .25s ease, box-shadow .25s ease; }
    html, body, [class*="css"] { font-family: 'Inter', sans-serif; -webkit-font-smoothing: antialiased; }
    .stApp, [data-testid="stAppViewContainer"] {
        background: radial-gradient(ellipse 80% 60% at 15% 0%, var(--bg-glow-1), transparent 70%),
                    radial-gradient(ellipse 70% 50% at 100% 100%, var(--bg-glow-2), transparent 70%),
                    linear-gradient(160deg, var(--bg-deep) 0%, var(--bg-mid) 100%);
        background-attachment: fixed; color: var(--text-main);
    }
    [data-testid="stHeader"] { background: transparent; height: 0; }
    [data-testid="stMain"] > div { padding-top: 3rem; padding-bottom: 4rem; }
    #MainMenu, footer { visibility: hidden; }
    [data-testid="stSidebar"] { background: var(--glass-bg); backdrop-filter: blur(30px); border-right: 1px solid var(--glass-border); }
    [data-testid="stSidebar"] > div { padding: 2rem 1.2rem; }
    [data-testid="stSidebar"] * { color: var(--text-main); }

    div[data-testid="InputInstructions"] { display: none !important; }

    h1 { font-family: 'Playfair Display', serif !important; font-weight: 600 !important; font-size: clamp(2.1rem, 3.5vw, 3rem); margin-bottom: 1.5rem; background: linear-gradient(135deg, var(--text-main) 0%, var(--accent) 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
    h2 { font-family: 'Inter', sans-serif !important; font-weight: 600 !important; color: var(--text-main) !important; margin-top: 2.4rem; border-bottom: 1px solid var(--panel-border); padding-bottom: .8rem; }
    h3 { font-family: 'Inter', sans-serif !important; font-weight: 600 !important; color: var(--text-main) !important; margin-top: 1.6rem; }
    p, label, .stMarkdown, span { color: var(--text-main); line-height: 1.6; }
    [data-testid="stCaptionContainer"] { color: var(--text-muted) !important; }

    div[data-testid="stMetric"], div[data-testid="stExpander"], div[data-testid="stForm"], div[data-testid="stAlert"], div[data-testid="stFileUploader"], div[data-testid="stImage"], .stChatMessage { background: var(--glass-bg); backdrop-filter: blur(20px); border: 1px solid var(--glass-border); border-radius: 24px; box-shadow: var(--shadow-soft); overflow: hidden; }
    div[data-testid="stImage"]:hover, div[data-testid="stExpander"]:hover, div[data-testid="stMetric"]:hover, .stChatMessage:hover { transform: translateY(-4px); }

    div[data-baseweb="input"] > div, div[data-baseweb="textarea"] > div, div[data-baseweb="select"] > div { background: var(--panel-bg) !important; border: 1px solid var(--glass-border) !important; border-radius: 14px !important; color: var(--text-main) !important; }
    div[data-baseweb="select"] * { color: var(--text-main) !important; cursor: pointer !important; }
    div[data-baseweb="select"] input { caret-color: transparent !important; pointer-events: none !important; }

    div.stButton > button, div.stDownloadButton > button, div[data-testid="stFormSubmitButton"] > button { 
        background: linear-gradient(135deg, var(--accent) 0%, var(--accent-2) 100%); 
        color: #ffffff !important; 
        border: 0; 
        border-radius: 14px; 
        padding: .75rem 1.5rem; 
        font-family: 'Montserrat', sans-serif !important; 
        font-size: 0.95rem !important; 
        font-weight: 600; 
        letter-spacing: 0.05em; 
        box-shadow: 0 10px 24px rgba(91, 141, 239, 0.24); 
    }
    div.stButton > button:hover { transform: translateY(-2px); box-shadow: 0 16px 36px rgba(91, 141, 239, 0.34); }

    [data-testid="stPills"] > label { display: none !important; }
    [data-testid="stPills"] div[role="radiogroup"] { display: flex !important; gap: .6rem !important; flex-wrap: wrap !important; }
    [data-testid="stPills"] button { background: var(--pill-bg) !important; border: 1.5px solid var(--pill-border) !important; border-radius: 14px !important; color: var(--pill-text) !important; padding: .7rem 1.4rem !important; font-weight: 500 !important; box-shadow: none !important; transition: all .22s !important; }
    [data-testid="stPills"] button:hover { border-color: var(--accent) !important; transform: translateY(-2px) !important; }
    [data-testid="stPills"] button[aria-checked="true"], [data-testid="stPills"] button[aria-pressed="true"] { background: linear-gradient(135deg, var(--accent), var(--accent-2)) !important; border-color: transparent !important; color: #ffffff !important; font-weight: 600 !important; }
    [data-testid="stPills"] button[aria-checked="false"] { background-color: var(--pill-bg) !important; background-image: none !important; color: var(--pill-text) !important; border: 1.5px solid var(--pill-border) !important; }

    /* --- БЛОК ЗАГРУЗКИ ФАЙЛА (ГРАДИЕНТНАЯ КНОПКА) --- */
    [data-testid="stFileUploader"] { 
        border: 2px dashed var(--glass-border) !important; 
        border-radius: 20px !important; 
        background-color: var(--panel-bg) !important; 
        padding: 10px !important; 
    }
    /* Стилизация кнопки внутри загрузчика */
    [data-testid="stFileUploader"] button {
        background: linear-gradient(135deg, var(--accent) 0%, var(--accent-2) 100%) !important;
        color: #ffffff !important;
        border: 0 !important;
        border-radius: 12px !important;
        padding: 0.5rem 1.2rem !important;
        font-family: 'Montserrat', sans-serif !important;
        font-size: 0.9rem !important;
        font-weight: 600 !important;
        letter-spacing: 0.05em !important;
        box-shadow: 0 4px 12px rgba(91, 141, 239, 0.24) !important;
        transition: all 0.2s !important;
    }
    [data-testid="stFileUploader"] button:hover {
        transform: translateY(-2px) !important;
        box-shadow: 0 8px 20px rgba(91, 141, 239, 0.4) !important;
    }

    hr { border: 0; height: 1px; background: linear-gradient(90deg, transparent, var(--panel-border), transparent); margin: 2rem 0; }

    @keyframes fadeUp { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }
    [data-testid="stMain"] > div > div > div { animation: fadeUp 0.6s ease-out; }

    .page-header { font-size: 0.9rem; color: var(--text-muted); margin-bottom: 2rem; }
    .page-subtitle { font-size: 0.75rem; font-weight: 700; letter-spacing: 0.1em; color: var(--accent); margin-bottom: 0.5rem; }
    .page-desc { font-size: 1.05rem; color: var(--text-muted); margin-bottom: 2.5rem; }

    .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 1.5rem; margin-bottom: 2rem; }
    .stats-card { background: var(--glass-bg); backdrop-filter: blur(20px); border: 1px solid var(--glass-border); border-radius: 24px; padding: 1.5rem; box-shadow: var(--shadow-soft); transition: transform 0.2s; }
    .stats-card:hover { transform: translateY(-4px); }
    .stats-top { display: flex; justify-content: space-between; align-items: center; color: var(--text-muted); font-weight: 500; margin-bottom: 1rem; }
    .icon-box { width: 32px; height: 32px; border-radius: 8px; background: rgba(99, 140, 255, 0.1); display: flex; align-items: center; justify-content: center; color: var(--text-muted); }
    .stats-value { font-family: 'Playfair Display', serif; font-size: 2.8rem; font-weight: 700; color: var(--text-main); line-height: 1; margin-bottom: 0.5rem; }
    .percent { font-size: 1.5rem; }
    .stats-sub { font-size: 0.85rem; color: #22c55e; }

    .chart-container h3 { margin-top: 0; margin-bottom: 1rem; font-family: 'Inter', sans-serif; font-size: 1.1rem; }

    .custom-chart { display: flex; align-items: flex-end; justify-content: space-around; height: 200px; background: var(--glass-bg); border: 1px solid var(--glass-border); border-radius: 20px; padding: 20px; margin-top: 1rem; gap: 10px; }
    .chart-bar-wrapper { display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; flex: 1; }
    .chart-bar { width: 100%; max-width: 40px; border-radius: 8px 8px 0 0; transition: height 0.5s ease; min-height: 5px; position: relative; }
    .chart-bar:hover { filter: brightness(1.2); }
    .chart-bar-value { font-size: 0.75rem; font-weight: 600; color: var(--text-main); margin-bottom: 5px; }
    .chart-bar-label { font-size: 0.7rem; color: var(--text-muted); margin-top: 8px; white-space: nowrap; }

    .history-item { display: flex; justify-content: space-between; align-items: center; background: var(--glass-bg); backdrop-filter: blur(20px); border: 1px solid var(--glass-border); border-radius: 20px; padding: 1.2rem 1.5rem; margin-bottom: 1rem; box-shadow: var(--shadow-soft); transition: all 0.2s; }
    .history-item:hover { transform: translateY(-2px); background: var(--glass-hover); }
    .history-title { font-weight: 600; font-size: 1.05rem; color: var(--text-main); margin-bottom: 0.3rem; }
    .history-sub { font-size: 0.85rem; color: var(--text-muted); }
    .history-right { font-family: 'Playfair Display', serif; font-size: 1.5rem; font-weight: 700; }
    .percent-good { color: #22c55e; }
    .percent-ok { color: #f59e0b; }
    .percent-bad { color: #ef4444; }

    .custom-card { background: var(--glass-bg); backdrop-filter: blur(20px); border: 1px solid var(--glass-border); border-radius: 24px; padding: 2rem; box-shadow: var(--shadow-soft); height: 100%; }
    div[data-testid="stFileUploader"] { margin-top: 1rem; margin-bottom: 1rem; }

    [data-testid="stSidebar"] .stButton > button { background: transparent !important; color: var(--text-main) !important; border: 1px solid var(--glass-border) !important; border-radius: 12px !important; box-shadow: none !important; text-align: left !important; justify-content: flex-start !important; padding: 10px 15px !important; font-family: 'Inter', sans-serif !important; font-size: 0.95rem !important; font-weight: 500 !important; margin-bottom: 0.5rem; }
    [data-testid="stSidebar"] .stButton > button:hover { background: rgba(99, 140, 255, 0.1) !important; color: var(--accent) !important; border-color: var(--accent) !important; }
    [data-testid="stSidebar"] .stButton > button:focus { background: rgba(99, 140, 255, 0.15) !important; color: var(--accent) !important; border-color: var(--accent) !important; box-shadow: none !important; }

    .question-badge { display: inline-block; background: rgba(99, 140, 255, 0.15); color: var(--accent); padding: 0.4rem 1rem; border-radius: 8px; font-size: 0.85rem; font-weight: 600; margin-bottom: 1rem; }

    div.stButton > button[kind="secondary"], div.stButton > button[kind="primary"] { min-height: 38px !important; padding: 0.35rem 0.9rem !important; font-size: 0.88rem !important; border-radius: 10px !important; font-weight: 500 !important; letter-spacing: 0 !important; box-shadow: none !important; transform: none !important; }
    div.stButton > button[kind="secondary"] { background: transparent !important; color: var(--text-muted) !important; border: 1px solid var(--glass-border) !important; }
    div.stButton > button[kind="secondary"]:hover { background: rgba(99,140,255,0.08) !important; color: var(--accent) !important; border-color: var(--accent) !important; box-shadow: none !important; }
    div.stButton > button[kind="primary"] { background: rgba(99,140,255,0.16) !important; color: var(--accent) !important; border: 1px solid rgba(99,140,255,0.45) !important; box-shadow: none !important; }
    div.stButton > button[kind="primary"]:hover { background: rgba(99,140,255,0.24) !important; color: var(--accent) !important; border-color: var(--accent) !important; box-shadow: none !important; }

    div.stDownloadButton > button, div[data-testid="stFormSubmitButton"] > button { min-height: 38px !important; padding: 0.35rem 0.9rem !important; font-size: 0.88rem !important; border-radius: 10px !important; font-weight: 500 !important; letter-spacing: 0 !important; box-shadow: none !important; transform: none !important; background: transparent !important; color: var(--text-muted) !important; border: 1px solid var(--glass-border) !important; }
    div.stDownloadButton > button:hover, div[data-testid="stFormSubmitButton"] > button:hover { background: rgba(99,140,255,0.08) !important; color: var(--accent) !important; border-color: var(--accent) !important; box-shadow: none !important; }

    div[data-testid="stAlert"] svg { display: none !important; }

    @media (max-width: 768px) {
        [data-testid="stMain"] > div { padding: 1.2rem .8rem 2rem; }
        h1 { font-size: 1.8rem; }
        .hero-title { font-size: 1.9rem; }
    }
    </style>
    """
    st.markdown(css, unsafe_allow_html=True)


def login_screen():
    st.markdown("<h1 style='text-align: center; margin-top: 5rem;'>Вход в систему</h1>", unsafe_allow_html=True)
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        with st.form("login_form"):
            username = st.text_input("Логин")
            password = st.text_input("Пароль", type="password")
            submitted = st.form_submit_button("Войти", use_container_width=True)
            
            if submitted:
                if username and password:
                    st.session_state.logged_in = True
                    st.session_state.username = username
                    
                    st.session_state.quiz_history = load_json_file(get_user_file("history"), [])
                    st.session_state.weak_data = load_json_file(get_user_file("weak"), [])
                    st.rerun()
                else:
                    st.error("Введите логин и пароль")


def main():
    st.set_page_config(page_title=APP_NAME, page_icon="📘", layout="wide", initial_sidebar_state="expanded")
    init_state()
    apply_theme()

    if not st.session_state.get("logged_in", False):
        login_screen()
        return

    st.sidebar.markdown(
        """
        <div style="padding:.8rem .6rem 1.6rem;">
            <div style="width:48px;height:48px;border-radius:16px;display:flex;align-items:center;justify-content:center;background:linear-gradient(135deg,#638cff,#a878ff);color:white;font-size:1.4rem;margin-bottom:1rem;box-shadow:0 12px 28px rgba(99,140,255,.35);">✦</div>
            <div style="font-family:'Playfair Display', serif; font-size:1.2rem;font-weight:600;color:var(--text-main);">AI Teacher</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    nav_items = [
        ("test", "Тест", "▤"),
        ("weak", "Слабые места", "◎"),
        ("stats", "Статистика", "▥"),
        ("history", "История", "◷"),
        ("settings", "Настройки", "⚙"),
        ("about", "О проекте", "ⓘ"),
    ]

    current = st.session_state.current_page
    for key, label, icon in nav_items:
        is_active = (current == key)

        if st.sidebar.button(
            f"{icon}  {label}",
            key=f"nav_{key}",
            use_container_width=True,
            type="primary" if is_active else "secondary",
        ):
            st.session_state.current_page = key
            st.rerun()

    if st.sidebar.button("Выйти", use_container_width=True):
        st.session_state.logged_in = False
        st.session_state.username = ""
        st.session_state.quiz_history = []
        st.session_state.weak_data = []
        st.rerun()

    if st.session_state.current_page == "test": page_test()
    elif st.session_state.current_page == "weak": page_weak()
    elif st.session_state.current_page == "stats": page_stats()
    elif st.session_state.current_page == "history": page_history()
    elif st.session_state.current_page == "settings": page_settings()
    else: page_about()


if __name__ == "__main__":
    main()