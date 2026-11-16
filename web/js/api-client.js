// Thin fetch wrapper: JSON in/out, 401 sends the user back to the login page.

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export async function api(path, { method = 'GET', body } = {}) {
  const response = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (response.status === 401 && !path.startsWith('/api/login')) {
    location.href = '/login.html';
    throw new ApiError(401, 'login required');
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch { /* non-JSON error body */ }
    throw new ApiError(response.status, detail);
  }
  return response;
}

export const getJson = async (path) => (await api(path)).json();
export const postJson = async (path, body) => (await api(path, { method: 'POST', body: body ?? {} })).json();

/** Download an authenticated binary endpoint through a temporary link. */
export async function saveFile(path, fallbackName) {
  const response = await api(path);
  const match = /filename="?([^";]+)"?/.exec(response.headers.get('content-disposition') ?? '');
  const link = document.createElement('a');
  link.href = URL.createObjectURL(await response.blob());
  link.download = match ? match[1] : fallbackName;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 10000);
}
