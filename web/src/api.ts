import type { components, paths } from './api-schema';

export type Identity = components['schemas']['IdentityResponse'];
export type Plan = components['schemas']['PlanResponse'];
export type Language = Identity['language'];
export type Store = {public_portal?: boolean; writes_enabled?: boolean; scope: string; slug: string | null; title: string; titles?: Record<string,string>; bot_username: string; support: Record<string, string>};
export type Account = components['schemas']['AccountResponse'];
export type Payment = components['schemas']['PaymentResponse'];
export type RenewalOptions = components['schemas']['RenewalOptionsResponse'];
export type PaymentMethod = components['schemas']['PaymentMethodResponse'];
export type Configuration = paths['/api/v1/accounts/{server_id}/{username}/configuration']['get']['responses'][200]['content']['application/json'];

let csrf = '';
export function setCsrf(value: string) { csrf = value; }
export class ApiError extends Error { constructor(message: string, public status: number) { super(message); } }

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  if (csrf) headers.set('X-CSRF-Token', csrf);
  const response = await fetch(`/api/v1${path}`, {...options, headers, credentials: 'same-origin'});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(typeof data.detail === 'string' ? data.detail : 'The request could not be completed.', response.status);
  return data;
}

export function safeLink(value?: string | null): string | undefined {
  if (!value) return;
  try { const url = new URL(value); if (url.protocol === 'https:') return url.href; } catch { /* Invalid public link. */ }
}
