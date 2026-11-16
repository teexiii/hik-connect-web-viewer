// Login page: posts credentials; credentials never touch browser storage.
const MESSAGES = {
  401: 'Sai tên đăng nhập hoặc mật khẩu.',
  429: 'Thử quá nhiều lần. Chờ vài phút rồi thử lại.',
};

const form = document.querySelector('#login');
const error = document.querySelector('#error');

function show(text) {
  error.textContent = text;
  error.hidden = !text;
}

fetch('/api/session', { credentials: 'same-origin' })
  .then((response) => response.json())
  .then((session) => { if (session.authenticated) location.href = '/'; })
  .catch(() => {});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = document.querySelector('#submit');
  button.disabled = true;
  show('');
  try {
    const response = await fetch('/api/login', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: form.username.value.trim(),
        password: form.password.value,
      }),
    });
    if (response.ok) {
      location.href = '/';
      return;
    }
    show(MESSAGES[response.status] ?? `Đăng nhập thất bại (${response.status}).`);
  } catch {
    show('Không kết nối được tới máy chủ.');
  } finally {
    button.disabled = false;
  }
});
