import { type Static, Type, type TSchema } from '@sinclair/typebox'
import { OpaqueIdSchema, PROTOCOL_VERSION, SequenceSchema, TimestampSchema } from './shared'

// New clipboard-text-v1 channel. Legacy control clipboard messages remain small.
// UI length is UTF-16 code units (the browser textarea's maxLength convention).
export const CLIPBOARD_TEXT_MAX_CHARS = 1_048_576
// Host -> viewer has its own fourfold limit; large copies use bounded chunks.
export const CLIPBOARD_TEXT_RECEIVE_MAX_CHARS = 65_536
export const CLIPBOARD_TEXT_MAX_BYTES = CLIPBOARD_TEXT_MAX_CHARS * 4
export const CLIPBOARD_TEXT_CHUNK_BYTES = 32 * 1024
const options = { additionalProperties: false } as const
const transfer = Type.Object({ transferId: OpaqueIdSchema }, options)
function envelope<T extends string, S extends TSchema>(event: T, data: S) {
  return Type.Object({ data, event: Type.Literal(event), sequence: SequenceSchema,
    sessionId: OpaqueIdSchema, timestamp: TimestampSchema, version: Type.Literal(PROTOCOL_VERSION) }, options)
}
export const TextClipboardMessageSchema = Type.Union([
  envelope('clipboard.text-offer', Type.Object({
    transferId: OpaqueIdSchema,
    encoding: Type.Literal('utf-8'),
    chars: Type.Integer({ minimum: 1, maximum: CLIPBOARD_TEXT_MAX_CHARS }),
    size: Type.Integer({ minimum: 1, maximum: CLIPBOARD_TEXT_MAX_BYTES }),
    sha256: Type.String({ pattern: '^[0-9a-f]{64}$' }),
  }, options)),
  envelope('clipboard.text-accept', transfer),
  envelope('clipboard.text-complete', transfer),
  envelope('clipboard.text-applied', transfer),
  envelope('clipboard.text-cancel', transfer),
  envelope('clipboard.text-error', Type.Object({
    transferId: OpaqueIdSchema, code: Type.String({ maxLength: 48, pattern: '^[A-Z_]+$' }),
  }, options)),
], { $id: 'TextClipboardMessageV1' })
export type TextClipboardMessage = Static<typeof TextClipboardMessageSchema>
