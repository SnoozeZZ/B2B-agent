const SESSION_TOKEN_KEY = "b2b.access_token.session";
const LOCAL_TOKEN_KEY = "b2b.access_token.local";
const AUTH_EXPIRED_EVENT = "b2b:auth-expired";

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function getAccessToken(): string | null {
  return sessionStorage.getItem(SESSION_TOKEN_KEY)
    ?? localStorage.getItem(LOCAL_TOKEN_KEY);
}

export function setAccessToken(token: string, remember: boolean): void {
  clearAccessToken();
  (remember ? localStorage : sessionStorage).setItem(
    remember ? LOCAL_TOKEN_KEY : SESSION_TOKEN_KEY,
    token,
  );
}

export function clearAccessToken(): void {
  sessionStorage.removeItem(SESSION_TOKEN_KEY);
  localStorage.removeItem(LOCAL_TOKEN_KEY);
}

export function onAuthExpired(handler: () => void): () => void {
  window.addEventListener(AUTH_EXPIRED_EVENT, handler);
  return () => window.removeEventListener(AUTH_EXPIRED_EVENT, handler);
}

async function errorFromResponse(response: Response): Promise<ApiError> {
  let message = `请求失败 (${response.status})`;
  try {
    const body = (await response.json()) as { detail?: string | Array<{ msg?: string }> };
    if (typeof body.detail === "string") message = body.detail;
    if (Array.isArray(body.detail)) {
      message = body.detail.map((item) => item.msg).filter(Boolean).join("；") || message;
    }
  } catch {
    // Keep the status-based message for non-JSON errors.
  }
  return new ApiError(message, response.status);
}

async function request(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  const token = getAccessToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const response = await fetch(`/api${path}`, { ...init, headers });
  if (!response.ok) {
    const error = await errorFromResponse(response);
    if (response.status === 401 && path !== "/auth/login") {
      clearAccessToken();
      window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
    }
    throw error;
  }
  return response;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await request(path, init);
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

function filenameFromDisposition(value: string | null): string | null {
  if (!value) return null;
  const encoded = value.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  if (encoded) {
    try {
      return decodeURIComponent(encoded.replace(/["']/g, ""));
    } catch {
      return encoded;
    }
  }
  return value.match(/filename="?([^";]+)"?/i)?.[1] ?? null;
}

export async function downloadFile(path: string, fallbackFilename: string): Promise<void> {
  const response = await request(path);
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filenameFromDisposition(response.headers.get("Content-Disposition")) ?? fallbackFilename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
