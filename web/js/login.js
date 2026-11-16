// Login page: posts credentials; credentials never touch browser storage.
const MESSAGES = {
  401: 'Sai tài khoản hoặc mật khẩu Hik-Connect.',
  409: 'Máy chủ đã gắn với tài khoản khác. Đăng nhập bằng tài khoản đó hoặc xoá tài khoản cũ.',
  423: 'Hik-Connect yêu cầu CAPTCHA: mở app Hik-Connect đăng nhập một lần rồi thử lại.',
  429: 'Thử quá nhiều lần. Chờ vài phút rồi thử lại.',
  502: 'Không kết nối được tới Hik-Connect. Thử lại sau.',
};
const REASONS = {
  needs_login: 'Phiên Hik-Connect hết hạn hoặc mật khẩu đã đổi. Đăng nhập lại.',
  captcha: MESSAGES[423],
};

const form = document.querySelector('#login');
const error = document.querySelector('#error');

function show(text) {
  error.textContent = text;
  error.hidden = !text;
}

show(REASONS[new URLSearchParams(location.search).get('reason')] ?? '');

fetch('/api/session', { credentials: 'same-origin' })
  .then((response) => response.json())
  .then((session) => { if (session.authenticated && session.account_status === 'ok') location.href = '/'; })
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
