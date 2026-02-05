import os
import time
import uuid
import json
import re
import base64
import requests
import streamlit as st
import threading
from queue import Queue
import hashlib

# ======================
# GIGACHAT AUTH
# ======================

CLIENT_ID = os.getenv("GIGACHAT_CLIENT_ID")
CLIENT_SECRET = os.getenv("GIGACHAT_CLIENT_SECRET")

if not CLIENT_ID or not CLIENT_SECRET:
    st.error("❌ Укажите GIGACHAT_CLIENT_ID и GIGACHAT_CLIENT_SECRET в Secrets")
    st.stop()

# Кэш access_token
_access_token = None
_token_expires_at = 0

# ======================
# ОЧЕРЕДЬ ЗАПРОСОВ
# ======================
class GigaChatQueue:
    """Очередь запросов для ограничения 1 одновременного запроса"""
    def __init__(self):
        self.request_queue = Queue()  # Очередь запросов
        self.result_dict = {}  # Словарь результатов {request_id: result}
        self.current_id = 0
        self.lock = threading.Lock()
        self.processing = False
        self.worker_thread = None
        self.start_worker()

    def start_worker(self):
        """Запускает рабочий поток для обработки очереди"""
        if self.worker_thread is None or not self.worker_thread.is_alive():
            self.worker_thread = threading.Thread(target=self._queue_worker, daemon=True)
            self.worker_thread.start()

    def add_request(self, func, *args, **kwargs):
        """Добавляет запрос в очередь и возвращает результат"""
        with self.lock:
            request_id = self.current_id
            self.current_id += 1

        # Добавляем запрос в очередь
        self.request_queue.put((request_id, func, args, kwargs))

        # Ждем результат (с таймаутом 60 секунд)
        start_time = time.time()
        while time.time() - start_time < 60:
            with self.lock:
                if request_id in self.result_dict:
                    result = self.result_dict.pop(request_id)
                    if isinstance(result, Exception):
                        raise result
                    return result
            time.sleep(0.1)

        raise TimeoutError("Таймаут ожидания ответа от GigaChat")

    def _queue_worker(self):
        """Рабочий поток, обрабатывающий очередь"""
        while True:
            # Берем запрос из очереди
            request_id, func, args, kwargs = self.request_queue.get()

            try:
                # Выполняем запрос
                result = func(*args, **kwargs)
            except Exception as e:
                result = e

            # Сохраняем результат
            with self.lock:
                self.result_dict[request_id] = result

            # Помечаем задачу как выполненную
            self.request_queue.task_done()

            # Небольшая пауза между запросами
            time.sleep(0.1)

# Создаем глобальную очередь
gigachat_queue = GigaChatQueue()

# ======================
# КЭШИРОВАНИЕ ОТВЕТОВ
# ======================
response_cache = {}
cache_lock = threading.Lock()

def get_cache_key(messages, model, max_tokens, temperature):
    """Создает ключ для кэша"""
    content = json.dumps(messages, sort_keys=True) + model + str(max_tokens) + str(temperature)
    return hashlib.md5(content.encode()).hexdigest()

# ======================
# GIGACHAT ФУНКЦИИ
# ======================
def get_gigachat_access_token():
    """Получает access_token с использованием client_id + client_secret."""
    global _access_token, _token_expires_at

    if _access_token and time.time() < _token_expires_at - 60:
        return _access_token

    credentials = f"{CLIENT_ID}:{CLIENT_SECRET}"
    encoded_credentials = base64.b64encode(credentials.encode('utf-8')).decode('utf-8')

    url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "RqUID": str(uuid.uuid4()),
        "Authorization": f"Basic {encoded_credentials}"
    }
    data = {"scope": "GIGACHAT_API_PERS"}

    try:
        response = requests.post(url, headers=headers, data=data, verify=False, timeout=30)
        response.raise_for_status()
        token_data = response.json()
        _access_token = token_data["access_token"]

        if "expires_at" in token_data:
            _token_expires_at = token_data["expires_at"]
        else:
            _token_expires_at = time.time() + 1800

        return _access_token
    except Exception as e:
        raise Exception(f"Ошибка получения токена: {str(e)}")

def call_gigachat_direct(messages, model="GigaChat-Max", max_tokens=1024, temperature=0.7):
    """Прямой вызов GigaChat API (без очереди)"""
    token = get_gigachat_access_token()
    url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    try:
        response = requests.post(url, headers=headers, json=payload, verify=False, timeout=60)

        if response.status_code == 401:
            global _access_token
            _access_token = None
            token = get_gigachat_access_token()
            headers["Authorization"] = f"Bearer {token}"
            response = requests.post(url, headers=headers, json=payload, verify=False)

        response.raise_for_status()
        result = response.json()
        return result["choices"][0]["message"]["content"]

    except Exception as e:
        raise Exception(f"GigaChat API ошибка: {str(e)}")

def call_gigachat(messages, model="GigaChat-Max", max_tokens=1024, temperature=0.7):
    """Вызов GigaChat через очередь с кэшированием"""
    # Проверяем кэш
    cache_key = get_cache_key(messages, model, max_tokens, temperature)
    with cache_lock:
        if cache_key in response_cache:
            return response_cache[cache_key]

    # Если нет в кэше, добавляем в очередь
    result = gigachat_queue.add_request(
        call_gigachat_direct,
        messages,
        model,
        max_tokens,
        temperature
    )

    # Сохраняем в кэш (только успешные ответы)
    if not isinstance(result, Exception):
        with cache_lock:
            response_cache[cache_key] = result

    return result

# ======================
# BOT FUNCTIONS
# ======================
def create_test(topic: str, explained_content: str, num_questions: int = 5, user_profile: dict = None):
    profile_str = ""
    if user_profile:
        parts = []
        if user_profile.get("level"): parts.append(f"уровень: {user_profile['level']}")
        if user_profile.get("goal"): parts.append(f"цель: {user_profile['goal']}")
        if user_profile.get("style"): parts.append(f"стиль: {user_profile['style']}")
        if user_profile.get("subject"): parts.append(f"предмет: {user_profile['subject']}")
        if parts:
            profile_str = f"\nУчёт профиля пользователя: {'; '.join(parts)}."

    difficulty = ""
    if user_profile and user_profile.get("goal") == "олимпиады":
        difficulty = "\nСделай вопросы повышенной сложности, как на региональной олимпиаде."
    elif user_profile and user_profile.get("goal") == "подготовка к ЕГЭ/ОГЭ":
        difficulty = "\nСделай вопросы в формате ЕГЭ/ОГЭ."

    prompt = f"""Создай тест по теме '{topic}' с {num_questions} вопросами.{profile_str}{difficulty}

    Материал для теста:
    {explained_content}

    Вопросы должны проверять понимание ЭТОГО МАТЕРИАЛА.
    НЕ задавай общие вопросы.

    Ответь СТРОГО в формате JSON:
    {{
        "questions": [
            {{
                "text": "текст вопроса",
                "options": ["вариант 1", "вариант 2", "вариант 3", "вариант 4"],
                "correct_answer": 0,
                "hint": "подсказка",
                "explanation": "почему этот ответ правильный"
            }}
        ]
    }}"""

    for attempt in range(2):
        try:
            raw_content = call_gigachat(
                messages=[{"role": "user", "content": prompt}],
                model="GigaChat-Max",
                max_tokens=1000,
                temperature=0.3
            )
            raw_content = re.sub(r'^```json\s*|\s*```$', '', raw_content.strip(), flags=re.MULTILINE)
            parsed = json.loads(raw_content)
            return json.dumps(parsed, ensure_ascii=False)
        except (json.JSONDecodeError, Exception) as e:
            if attempt == 0:
                prompt += "\n\nОТВЕЧАЙ ТОЛЬКО ВАЛИДНЫМ JSON БЕЗ ЛЮБОГО ДРУГОГО ТЕКСТА."
                continue
            else:
                raise Exception(f"Не удалось получить валидный JSON: {str(e)}")

def get_ai_response(messages, user_profile: dict = None):
    messages_for_api = []
    for msg in messages:
        if msg["role"] == "user" and user_profile:
            content = msg["content"]
            parts = []
            if user_profile.get("level"): parts.append(f"уровень: {user_profile['level']}")
            if user_profile.get("goal"): parts.append(f"цель: {user_profile['goal']}")
            if user_profile.get("style"): parts.append(f"стиль: {user_profile['style']}")
            if user_profile.get("subject"): parts.append(f"предмет: {user_profile['subject']}")
            if parts:
                content += f"\n\n[Профиль: {'; '.join(parts)}]"
            messages_for_api.append({"role": msg["role"], "content": content})
        else:
            messages_for_api.append(msg)

    if len(messages_for_api) > 6:
        messages_for_api = [messages_for_api[0]] + messages_for_api[-5:]

    return call_gigachat(
        messages=messages_for_api,
        model="GigaChat-Max",
        max_tokens=800,
        temperature=0.6
    )

def wants_test(user_input):
    user_lower = user_input.lower().strip()
    topic_match = re.search(r'(?:тест|проверь\s+знания|проверить\s+знания)\s+по\s+(.+)', user_lower)
    if topic_match:
        return True, topic_match.group(1).strip()

    explicit_phrases = [
        'создай тест', 'давай тест', 'хочу тест', 'сделай тест',
        'проверь знания', 'пройти тест', 'начать тест', 'запусти тест'
    ]
    short_commands = ['тест', 'quiz', 'проверь', 'проверка']

    if any(phrase in user_lower for phrase in explicit_phrases) or user_lower in short_commands:
        return True, None

    return False, None

def wants_error_review(user_input):
    review_keywords = ['разбер', 'ошибк', 'неправильн', 'объясни', 'почему']
    return any(keyword in user_input.lower() for keyword in review_keywords)

# ======================
# STREAMLIT APP
# ======================

if 'messages' not in st.session_state:
    st.session_state.messages = [{
        "role": "system",
        "content": """Ты — дружелюбный виртуальный помощник для школьников и студентов.
- Отвечай на русском языке, кратко и понятно.
- Объясняй сложные концепции простыми словами с примерами.
- После объяснения темы спроси: "Хотите проверить знания? Напишите 'тест' или 'проверить знания'".
- Если пользователь просит разобрать ошибки, дай подробное объяснение каждой ошибки.
- Будь поддерживающим и мотивируй на обучение."""
    }]

if 'last_test_result' not in st.session_state:
    st.session_state.last_test_result = None

if 'last_topic' not in st.session_state:
    st.session_state.last_topic = None

if 'last_explanation' not in st.session_state:
    st.session_state.last_explanation = None

if 'test_in_progress' not in st.session_state:
    st.session_state.test_in_progress = False

if 'user_profile' not in st.session_state:
    st.session_state.user_profile = {}

if 'session_test_scores' not in st.session_state:
    st.session_state.session_test_scores = []

if len(st.session_state.messages) == 1:
    welcome_msg = (
        "👋 Привет! Я — ваш ИИ-помощник по обучению.\n\n"
        "Напишите тему, которую хотите разобрать — например, «производная», «законы Ньютона».\n\n"
        "Или сразу запросите тест: «тест по тригонометрии».\n\n"
        "Заполните анкету в боковой панели, чтобы адаптировать уровень 👈"
    )
    st.session_state.messages.append({
        "role": "assistant",
        "content": welcome_msg
    })

st.set_page_config(page_title="Обучающий чат", page_icon="🎓", layout="centered")
st.title("🎓 Обучающий чат с ИИ-тестированием")
st.caption("Создано Хайруллиным Р.Р.")

# Sidebar
with st.sidebar:
    st.header("👤 Профиль")
    with st.expander("Заполнить анкету", expanded=False):
        level = st.selectbox("Класс / уровень", ["7–9 класс", "10–11 класс", "студент", "другое"])
        goal = st.selectbox("Цель использования", ["подготовка к ЕГЭ/ОГЭ", "олимпиады", "просто понять тему", "повторение"])
        style = st.selectbox("Стиль объяснений", ["очень просто, с примерами из жизни", "строго, с формулами и терминами"])
        subject = st.text_input("Предмет (например, алгебра, физика)")

        if st.button("Сохранить профиль", use_container_width=True):
            st.session_state.user_profile = {
                "level": level,
                "goal": goal,
                "style": style,
                "subject": subject if subject else None
            }
            st.success("Профиль сохранён!")

    st.divider()
    st.header("⚙️ Настройки теста")
    num_questions = st.slider("Количество вопросов", 3, 10, 5)
    show_hints = st.checkbox("Показывать подсказки", value=True)

    st.divider()

    if st.session_state.last_test_result:
        result = st.session_state.last_test_result
        score = result.get('score', 0)
        total = result.get('total', 0)
        if total > 0:
            percentage = (score / total) * 100
            st.metric("Правильных ответов", f"{score}/{total}", f"{percentage:.0f}%")

            if not st.session_state.session_test_scores or st.session_state.session_test_scores[-1] != percentage:
                st.session_state.session_test_scores.append(percentage)

            if percentage >= 90:
                st.success("🏆 Уровень: Эксперт")
            elif percentage >= 75:
                st.info("🎖️ Уровень: Уверенный")
            elif percentage >= 60:
                st.warning("📚 Уровень: Начинающий")
            else:
                st.error("🌱 Уровень: Новичок")

            if len(st.session_state.session_test_scores) >= 2 and percentage > st.session_state.session_test_scores[-2]:
                st.success("🚀 Ваш результат улучшился!")

    if len(st.session_state.session_test_scores) > 1:
        st.divider()
        st.header("📈 Прогресс")
        st.line_chart(st.session_state.session_test_scores, height=200)

    st.divider()
    if st.button("🔄 Новый чат", use_container_width=True):
        st.session_state.messages = [st.session_state.messages[0]]
        st.session_state.last_test_result = None
        st.session_state.last_topic = None
        st.session_state.last_explanation = None
        st.session_state.test_in_progress = False
        st.session_state.session_test_scores = []
        st.rerun()

def display_test(test_data_str, message_index):
    try:
        test_data = json.loads(test_data_str) if isinstance(test_data_str, str) else test_data_str
    except json.JSONDecodeError:
        st.error("Ошибка при загрузке теста.")
        return

    questions = test_data.get('questions', [])
    if not questions:
        st.warning("Тест пуст.")
        return

    answers_key = f"answers_{message_index}"
    submitted_key = f"submitted_{message_index}"
    hints_used_key = f"hints_{message_index}"

    if answers_key not in st.session_state:
        st.session_state[answers_key] = {}
    if submitted_key not in st.session_state:
        st.session_state[submitted_key] = False
    if hints_used_key not in st.session_state:
        st.session_state[hints_used_key] = set()

    if not st.session_state[submitted_key]:
        st.subheader("📝 Тест")
        progress = len(st.session_state[answers_key]) / len(questions)
        st.progress(progress, text=f"Отвечено: {len(st.session_state[answers_key])}/{len(questions)}")

        for i, question in enumerate(questions):
            with st.container():
                st.markdown(f"### Вопрос {i+1}")
                st.markdown(f"**{question['text']}**")

                if show_hints and question.get('hint'):
                    hint_key = f"show_hint_{message_index}_{i}"
                    if st.button(f"💡 Подсказка", key=hint_key):
                        st.session_state[hints_used_key].add(i)
                    if i in st.session_state[hints_used_key]:
                        st.info(f"💡 {question['hint']}")

                answer = st.radio(
                    "Выберите ответ:",
                    options=question['options'],
                    key=f"q_{message_index}_{i}",
                    index=None,
                    label_visibility="collapsed"
                )
                if answer:
                    st.session_state[answers_key][i] = question['options'].index(answer)
                st.divider()

        col1, col2 = st.columns(2)
        with col1:
            if len(st.session_state[answers_key]) == len(questions):
                if st.button("✅ Проверить ответы", type="primary", use_container_width=True):
                    st.session_state[submitted_key] = True
                    correct_count = sum(
                        1 for i, q in enumerate(questions)
                        if st.session_state[answers_key].get(i) == q['correct_answer']
                    )
                    st.session_state.last_test_result = {
                        'test_data': test_data,
                        'user_answers': st.session_state[answers_key].copy(),
                        'message_index': message_index,
                        'score': correct_count,
                        'total': len(questions),
                        'hints_used': len(st.session_state[hints_used_key])
                    }
                    st.session_state.test_in_progress = False
                    st.rerun()
    else:
        st.subheader("📊 Результаты")
        correct_count = 0
        for i, question in enumerate(questions):
            user_answer = st.session_state[answers_key].get(i)
            correct_answer = question['correct_answer']
            with st.container():
                if user_answer == correct_answer:
                    st.success(f"✓ **Вопрос {i+1}:** {question['text']}")
                    st.markdown(f"Ваш ответ: **{question['options'][user_answer]}** ✓")
                    correct_count += 1
                else:
                    st.error(f"✗ **Вопрос {i+1}:** {question['text']}")
                    if user_answer is not None:
                        st.markdown(f"Ваш ответ: ~~{question['options'][user_answer]}~~")
                    st.markdown(f"Правильный ответ: **{question['options'][correct_answer]}**")
                    if question.get('explanation'):
                        with st.expander("📖 Объяснение"):
                            st.write(question['explanation'])
                st.divider()

        hints_count = len(st.session_state.get(hints_used_key, set()))
        score_percent = (correct_count / len(questions)) * 100

        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Правильных", f"{correct_count}/{len(questions)}")
        with col2:
            st.metric("Результат", f"{score_percent:.0f}%")
        with col3:
            st.metric("Подсказок", str(hints_count))

        if score_percent >= 80:
            st.balloons()
            st.success("🌟 Отличная работа! Вы отлично усвоили материал!")
        elif score_percent >= 60:
            st.info("👍 Хороший результат! Ещё немного практики — и будет идеально!")
        else:
            st.warning("📚 Не расстраивайтесь! Напишите 'разбери ошибки' для подробного объяснения.")

# Display chat history
for idx, msg in enumerate(st.session_state.messages):
    if msg['role'] == 'system':
        continue
    if msg['role'] == 'user':
        with st.chat_message('user'):
            st.write(msg['content'])
    elif msg['role'] == 'assistant':
        if msg.get('content') and msg['content'].strip():
            with st.chat_message('assistant'):
                st.write(msg['content'])
    elif msg['role'] == 'test':
        with st.chat_message('assistant'):
            display_test(msg['test_data'], idx)

# Handle user input
user_input = st.chat_input("Например: «тест по квадратным уравнениям»...")

if user_input:
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.write(user_input)

    is_test_request, requested_topic = wants_test(user_input)

    if is_test_request:
        with st.chat_message("assistant"):
            with st.spinner("🧠 Создаю тест..."):
                try:
                    if requested_topic:
                        explanation_prompt = f"Кратко объясни тему '{requested_topic}' для школьника. Дай определения и формулы. Не задавай вопросов."
                        explanation_messages = [
                            {"role": "system", "content": "Ты учитель. Объясняй чётко."},
                            {"role": "user", "content": explanation_prompt}
                        ]
                        explained_content = get_ai_response(explanation_messages)
                        test_result = create_test(
                            topic=requested_topic,
                            explained_content=explained_content,
                            num_questions=num_questions,
                            user_profile=st.session_state.user_profile
                        )
                        parsed_test = json.loads(test_result)
                        st.session_state.messages.append({"role": "test", "test_data": parsed_test})
                        st.session_state.last_topic = requested_topic
                        st.session_state.last_explanation = explained_content
                        st.session_state.test_in_progress = True
                        st.rerun()

                    elif st.session_state.last_explanation:
                        test_result = create_test(
                            topic=st.session_state.last_topic or "общая тема",
                            explained_content=st.session_state.last_explanation,
                            num_questions=num_questions,
                            user_profile=st.session_state.user_profile
                        )
                        parsed_test = json.loads(test_result)
                        st.session_state.messages.append({"role": "test", "test_data": parsed_test})
                        st.session_state.test_in_progress = True
                        st.rerun()

                    else:
                        msg = "Пожалуйста, сначала объясните тему или напишите «тест по [тема]»."
                        st.write(msg)
                        st.session_state.messages.append({"role": "assistant", "content": msg})

                except Exception as e:
                    error_msg = f"Ошибка: {str(e)}"
                    st.error(error_msg)
                    st.session_state.messages.append({"role": "assistant", "content": error_msg})

    elif wants_error_review(user_input) and st.session_state.last_test_result:
        test_result = st.session_state.last_test_result
        test_data = test_result['test_data']
        user_answers = test_result['user_answers']

        errors_info = []
        for i, question in enumerate(test_data['questions']):
            user_answer_idx = user_answers.get(i)
            correct_answer_idx = question['correct_answer']
            if user_answer_idx != correct_answer_idx:
                errors_info.append({
                    'question': question['text'],
                    'user_answer': question['options'][user_answer_idx] if user_answer_idx is not None else "Не отвечено",
                    'correct_answer': question['options'][correct_answer_idx],
                    'explanation': question.get('explanation', '')
                })

        if errors_info:
            explanation_request = (
                "Проанализируй ошибки пользователя и построй **мини-урок по типам ошибок**. "
                "Сгруппируй вопросы по общим темам и дай общие рекомендации. Не пересказывай объяснения из теста!\n\nОшибки:\n"
            )
            for i, error in enumerate(errors_info, 1):
                explanation_request += f"{i}. Вопрос: {error['question']}\n"
                explanation_request += f"   Неправильный ответ: {error['user_answer']}\n"
                explanation_request += f"   Правильный ответ: {error['correct_answer']}\n\n"

            with st.chat_message("assistant"):
                with st.spinner("📚 Анализирую ошибки..."):
                    try:
                        messages_for_api = [
                            {"role": "system", "content": "Ты эксперт-педагог. Объясняй ошибки структурированно."},
                            {"role": "user", "content": explanation_request}
                        ]
                        response = get_ai_response(messages_for_api, st.session_state.user_profile)
                        st.write(response)
                        st.session_state.messages.append({"role": "assistant", "content": response})
                    except Exception as e:
                        st.error(f"Ошибка при анализе: {str(e)}")
        else:
            msg = "🎉 В вашем последнем тесте не было ошибок! Отличная работа!"
            with st.chat_message("assistant"):
                st.write(msg)
            st.session_state.messages.append({"role": "assistant", "content": msg})

    else:
        with st.chat_message("assistant"):
            with st.spinner("💭 Думаю..."):
                try:
                    messages_for_api = [
                        msg for msg in st.session_state.messages 
                        if msg['role'] in ['system', 'user', 'assistant'] and msg.get('content')
                    ]
                    response = get_ai_response(messages_for_api, st.session_state.user_profile)
                    st.write(response)
                    st.session_state.messages.append({"role": "assistant", "content": response})
                    st.session_state.last_topic = user_input
                    st.session_state.last_explanation = response
                except Exception as e:
                    error_msg = f"Произошла ошибка: {str(e)}"
                    st.error(error_msg)
                    st.session_state.messages.append({"role": "assistant", "content": error_msg})
        st.rerun()
