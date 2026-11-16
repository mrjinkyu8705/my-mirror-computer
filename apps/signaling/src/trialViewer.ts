const encoder = new TextEncoder()

/** The isolated viewer receives only this derived key, not the Pages signing key. */
export async function deriveTrialViewerKey(secret: string, origin: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    'raw', encoder.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'],
  )
  const result = await crypto.subtle.sign(
    'HMAC', key, encoder.encode(`mirror.trial-viewer.ticket.v1\n${origin}`),
  )
  return Array.from(new Uint8Array(result), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

export function isTrialViewerOrigin(
  origin: string | null,
  config: { readonly viewerOrigin?: string | undefined; readonly trialViewerOrigin?: string | undefined },
): origin is string {
  return Boolean(config.viewerOrigin && config.trialViewerOrigin &&
    config.trialViewerOrigin !== config.viewerOrigin && origin === config.trialViewerOrigin)
}
