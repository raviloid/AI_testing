import os
from groq import Groq
import streamlit as st
import json
import re

# Инициализация Groq клиента
api_key = os.getenv("GROQ_API_KEY")
client = Groq(api_key=api_key)


# Функция для генерации теста
def create_test(topic: str,
                explained_content: str,
                num_questions: int = 5,
                user_profile: dict = None):
    profile_str = ""
    if user_profile:
        parts = []
        if user_profile.get("level"):
            parts.append(f"уровень: {user_profile['level']}")
        if user_profile.get("goal"):
            parts.append(f"цель: {user_profile['goal']}")
        if user_profile.get("style"):
            parts.append(f"стиль: {user_profile['style']}")
        if user_profile.get("subject"):
            parts.append(f"предмет: {user_profile['subject']}")
        if parts:
            profile_str = f"\nУчёт профиля пользователя: {'; '.join(parts)}."

    prompt = f"""Создай тест по теме '{topic}' с {num_questions} вопросами.{profile_str}

    Материал для теста:
    {explained_content}

    Вопросы должны проверять понимание этого материала.

    ВАЖНО: Ответь ТОЛЬКО валидным JSON без дополнительного текста.

    Формат:
    {{
        "questions": [
            {{
                "text": "текст вопроса",
                "options": ["вариант 1", "вариант 2", "вариант 3", "вариант 4"],
                "correct_answer": 0,
                "hint": "подсказка для этого вопроса",
                "explanation": "почему этот ответ правильный"
            }}
        ]
    }}"""

    for attempt in range(2):  # Повторить максимум 1 раз
        try:
            response = client.chat.completions.create(
                messages=[{
                    "role": "user",
                    "content": prompt
                }],
                model="llama-3.3-70b-versatile",
                response_format={"type": "json_object"},
            )
            raw_content = response.choices[0].message.content
            # Проверяем валидность JSON сразу
            json.loads(raw_content)
            return raw_content
        except json.JSONDecodeError:
            if attempt == 0:
                prompt += "\n\nОШИБКА: предыдущий ответ не был валидным JSON. Ответь СТРОГО по формату."
                continue
            else:
                raise Exception("LLM дважды вернул невалидный JSON")
    raise Exception("Не удалось получить валидный ответ от LLM")


# Функция для получения ответа от AI
def get_ai_response(messages, user_profile: dict = None):
    # Добавляем профиль в последнее сообщение пользователя (если есть)
    messages_for_api = []
    for msg in messages:
        if msg["role"] == "user" and user_profile:
            content = msg["content"]
            parts = []
            if user_profile.get("level"):
                parts.append(f"уровень: {user_profile['level']}")
            if user_profile.get("goal"):
                parts.append(f"цель: {user_profile['goal']}")
            if user_profile.get("style"):
                parts.append(f"стиль: {user_profile['style']}")
            if user_profile.get("subject"):
                parts.append(f"предмет: {user_profile['subject']}")
            if parts:
                content += f"\n\n[Профиль: {'; '.join(parts)}]"
            messages_for_api.append({"role": msg["role"], "content": content})
        else:
            messages_for_api.append(msg)

    # Ограничиваем контекст до последних 6 сообщений (3 пары)
    if len(messages_for_api) > 6:
        messages_for_api = [
            messages_for_api[0]
        ] + messages_for_api[-5:]  # Сохраняем system + последние 5

    response = client.chat.completions.create(model="llama-3.3-70b-versatile",
                                              messages=messages_for_api)
    return response.choices[0].message.content


# Функция для определения, хочет ли пользователь тест
def wants_test(user_input):
    user_lower = user_input.lower().strip()
    explicit_test_phrases = [
        'создай тест', 'давай тест', 'хочу тест', 'сделай тест',
        'проверь знания', 'проверить знания', 'пройти тест', 'начать тест',
        'запусти тест', 'тест по теме'
    ]
    short_commands = ['тест', 'quiz', 'проверь', 'проверка']
    if any(phrase in user_lower for phrase in explicit_test_phrases):
        return True
    if user_lower in short_commands or user_lower.startswith(
            tuple(c + ' ' for c in short_commands)):
        return True
    return False


# Функция для определения, хочет ли пользователь разбор ошибок
def wants_error_review(user_input):
    review_keywords = ['разбер', 'ошибк', 'неправильн', 'объясни', 'почему']
    return any(keyword in user_input.lower() for keyword in review_keywords)


# Инициализация состояния
if 'messages' not in st.session_state:
    st.session_state.messages = [{
        "role":
        "system",
        "content":
        """Ты — дружелюбный виртуальный помощник для школьников и студентов.

        ОСНОВНЫЕ ПРАВИЛА:
        1. Отвечай на русском языке, кратко и понятно.
        2. Объясняй сложные концепции простыми словами с примерами.
        3. Если пользователь просто поздоровался (например: "привет", "здравствуйте", "hi"), НЕ начинай объяснять тему!
           Вместо этого скажи что-то вроде:
           - Если известен его уровень/предмет: "Привет! Готов разобрать [предмет] для [уровень]? Напиши тему!"
           - Если профиль не заполнен: "Привет! Напиши, что хочешь изучить, или заполни анкету в боковой панели."
        4. Только после того, как пользователь задаст конкретную тему, дай объяснение.
        5. После объяснения спроси: "Хотите проверить знания? Напишите 'тест' или 'проверить знания'."
        6. Если просят разобрать ошибки — строй мини-урок по типам ошибок, а не повторяй объяснения.
        7. Будь поддерживающим, мотивируй, но не навязчиво.
        """
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
    st.session_state.session_test_scores = []  # Список процентов

# Настройки страницы
st.set_page_config(page_title="Обучающий чат",
                   page_icon="🎓",
                   layout="centered")

# Добавляем CSS для плавных кнопок
st.markdown("""
<style>
button {
    transition: background-color 0.3s ease, color 0.3s ease;
}
button:hover {
    filter: brightness(1.1);
}
</style>
""",
            unsafe_allow_html=True)

# Заголовок
st.title("🎓 Обучающий чат с тестированием")
st.caption("Создано Хайруллиным Р.Р.")

# Боковая панель с анкетой и настройками
# Боковая панель с анкетой и настройками
with st.sidebar:
    st.header("👤 Профиль")
    with st.expander("Заполнить анкету", expanded=False):
        level = st.selectbox("Класс / уровень", ["7–9 класс", "10–11 класс", "студент", "другое"])
        goal = st.selectbox("Цель использования", ["подготовка к ЕГЭ/ОГЭ", "олимпиады", "просто понять тему", "повторение"])
        style = st.selectbox("Стиль объяснений", ["очень просто, с примерами из жизни", "строго, с формулами и терминами", "как учитель, но без занудства"])
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
            st.metric("Правильных ответов", f"{score}/{total}",
                      f"{percentage:.0f}%")

            # Сохраняем результат в историю сессии
            if len(
                    st.session_state.session_test_scores
            ) == 0 or st.session_state.session_test_scores[-1] != percentage:
                st.session_state.session_test_scores.append(percentage)

            # Проверяем улучшение
            if len(
                    st.session_state.session_test_scores
            ) >= 2 and percentage > st.session_state.session_test_scores[-2]:
                st.success("🚀 Ваш результат улучшился! Отличная работа!")

            if percentage >= 80:
                st.success("🌟 Отлично!")
            elif percentage >= 60:
                st.info("👍 Хорошо!")
            else:
                st.warning("📚 Нужно повторить материал")

    # График прогресса
    if len(st.session_state.session_test_scores) > 1:
        st.divider()
        st.header("📈 Прогресс в сессии")
        st.line_chart(st.session_state.session_test_scores, height=200)

    st.divider()
    if st.button("🔄 Новый чат", use_container_width=True):
        st.session_state.messages = [{
            "role":
            "system",
            "content":
            st.session_state.messages[0]["content"]
        }]
        st.session_state.last_test_result = None
        st.session_state.last_topic = None
        st.session_state.last_explanation = None
        st.session_state.test_in_progress = False
        st.session_state.session_test_scores = []
        st.rerun()


# Функция для отображения теста (без изменений, кроме ключей)
def display_test(test_data_str, message_index):
    try:
        test_data = json.loads(test_data_str) if isinstance(
            test_data_str, str) else test_data_str
    except json.JSONDecodeError:
        st.error("Ошибка при загрузке теста. Попробуйте создать новый.")
        return

    questions = test_data.get('questions', [])
    if not questions:
        st.warning("Тест пуст. Попробуйте создать новый.")
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
        st.progress(
            progress,
            text=
            f"Отвечено: {len(st.session_state[answers_key])}/{len(questions)}")

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

                answer = st.radio("Выберите ответ:",
                                  options=question['options'],
                                  key=f"q_{message_index}_{i}",
                                  index=None,
                                  label_visibility="collapsed")

                if answer:
                    st.session_state[answers_key][i] = question[
                        'options'].index(answer)

                st.divider()

        col1, col2 = st.columns(2)
        with col1:
            if len(st.session_state[answers_key]) == len(questions):
                if st.button("✅ Проверить ответы",
                             type="primary",
                             use_container_width=True):
                    st.session_state[submitted_key] = True

                    correct_count = sum(1 for i, q in enumerate(questions)
                                        if st.session_state[answers_key].get(i)
                                        == q['correct_answer'])

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
        st.subheader("📊 Результаты теста")

        correct_count = 0
        for i, question in enumerate(questions):
            user_answer = st.session_state[answers_key].get(i)
            correct_answer = question['correct_answer']

            with st.container():
                if user_answer == correct_answer:
                    st.success(f"✓ **Вопрос {i+1}:** {question['text']}")
                    st.markdown(
                        f"Ваш ответ: **{question['options'][user_answer]}** ✓")
                    correct_count += 1
                else:
                    st.error(f"✗ **Вопрос {i+1}:** {question['text']}")
                    if user_answer is not None:
                        st.markdown(
                            f"Ваш ответ: ~~{question['options'][user_answer]}~~"
                        )
                    st.markdown(
                        f"Правильный ответ: **{question['options'][correct_answer]}**"
                    )

                    if question.get('explanation'):
                        with st.expander("📖 Объяснение"):
                            st.write(question['explanation'])

                st.divider()

        # Итоговая статистика в одну строку
        hints_count = len(st.session_state.get(hints_used_key, set()))
        score_percent = (correct_count / len(questions)) * 100
        st.markdown(
            f"✅ **Правильных:** {correct_count}/{len(questions)} &nbsp; | &nbsp; "
            f"📊 **Результат:** {score_percent:.0f}% &nbsp; | &nbsp; "
            f"💡 **Подсказок:** {hints_count}"
        )

        if score >= 80:
            st.balloons()
            st.success("🌟 Отличная работа! Вы отлично усвоили материал!")
        elif score >= 60:
            st.info(
                "👍 Хороший результат! Ещё немного практики, и будет идеально!")
        else:
            st.warning(
                "📚 Не расстраивайтесь! Напишите 'разбери ошибки' для подробного объяснения."
            )


# Отображение истории сообщений
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

# Ввод пользователя
user_input = st.chat_input(
    "Напишите свой вопрос или 'тест' для проверки знаний...")

if user_input:
    st.session_state.messages.append({"role": "user", "content": user_input})

    with st.chat_message("user"):
        st.write(user_input)

    if wants_test(user_input) and st.session_state.last_explanation:
        with st.chat_message("assistant"):
            with st.spinner("🧠 Создаю тест..."):
                try:
                    test_result = create_test(
                        topic=st.session_state.last_topic or "общая тема",
                        explained_content=st.session_state.last_explanation,
                        num_questions=num_questions,
                        user_profile=st.session_state.user_profile)

                    parsed_test = json.loads(test_result)
                    st.session_state.messages.append({
                        "role": "test",
                        "test_data": parsed_test
                    })
                    st.session_state.test_in_progress = True
                    st.rerun()

                except Exception as e:
                    error_msg = f"Не удалось создать тест. Попробуйте ещё раз. Ошибка: {str(e)}"
                    st.error(error_msg)
                    st.session_state.messages.append({
                        "role": "assistant",
                        "content": error_msg
                    })

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
                    'question':
                    question['text'],
                    'user_answer':
                    question['options'][user_answer_idx]
                    if user_answer_idx is not None else "Не отвечено",
                    'correct_answer':
                    question['options'][correct_answer_idx],
                    'explanation':
                    question.get('explanation', '')
                })

        if errors_info:
            explanation_request = f"Построй мини-урок по типам ошибок из теста. Не просто пересказывай объяснения, а группируй ошибки по темам и давай общие рекомендации.\n\nОшибки:\n"
            for i, error in enumerate(errors_info, 1):
                explanation_request += f"{i}. Вопрос: {error['question']}\n"
                explanation_request += f"   Неправильный ответ: {error['user_answer']}\n"
                explanation_request += f"   Правильный ответ: {error['correct_answer']}\n\n"

            with st.chat_message("assistant"):
                with st.spinner("📚 Анализирую ошибки..."):
                    try:
                        messages_for_api = [{
                            "role":
                            "system",
                            "content":
                            "Ты помощник по обучению. Объясняй ошибки подробно и понятно, с примерами."
                        }, {
                            "role": "user",
                            "content": explanation_request
                        }]

                        response = get_ai_response(
                            messages_for_api, st.session_state.user_profile)
                        st.write(response)
                        st.session_state.messages.append({
                            "role": "assistant",
                            "content": response
                        })
                    except Exception as e:
                        error_msg = f"Ошибка при анализе: {str(e)}"
                        st.error(error_msg)
        else:
            no_errors_msg = "🎉 В вашем последнем тесте не было ошибок! Отличная работа!"
            with st.chat_message("assistant"):
                st.write(no_errors_msg)
            st.session_state.messages.append({
                "role": "assistant",
                "content": no_errors_msg
            })

    else:
        with st.chat_message("assistant"):
            with st.spinner("💭 Думаю..."):
                try:
                    messages_for_api = [
                        msg for msg in st.session_state.messages
                        if msg['role'] in ['system', 'user', 'assistant']
                        and msg.get('content')
                    ]

                    response = get_ai_response(messages_for_api,
                                               st.session_state.user_profile)
                    st.write(response)

                    st.session_state.messages.append({
                        "role": "assistant",
                        "content": response
                    })

                    st.session_state.last_topic = user_input
                    st.session_state.last_explanation = response

                except Exception as e:
                    error_msg = f"Произошла ошибка: {str(e)}"
                    st.error(error_msg)
                    st.session_state.messages.append({
                        "role": "assistant",
                        "content": error_msg
                    })

    st.rerun()
