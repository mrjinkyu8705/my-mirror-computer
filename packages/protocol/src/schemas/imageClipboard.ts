import { type Static, Type, type TSchema } from '@sinclair/typebox'

import {
  OpaqueIdSchema,
  PROTOCOL_VERSION,
  SequenceSchema,
  TimestampSchema,
} from './shared'

// Image clipboard payloads travel as raw chunks on clipboard-v1. Metadata and
// completion acknowledgements use these strict JSON envelopes. The dedicated
// channel keeps multi-megabyte screenshots away from mouse/keyboard control.
export const IMAGE_CLIPBOARD_EVENTS = [
  'clipboard.image-offer',
  'clipboard.image-complete',
  'clipboard.image-applied',
  'clipboard.image-error',
] as const

export const CLIPBOARD_IMAGE_MAX_BYTES = 20 * 1024 * 1024
const Sha256Schema = Type.String({ pattern: '^[0-9a-f]{64}$' })
const TransferIdSchema = OpaqueIdSchema
const ErrorCodeSchema = Type.String({
  maxLength: 48,
  pattern: '^[A-Z_]+$',
})
const messageOptions = { additionalProperties: false } as const

function createEnvelope<TEvent extends string, TData extends TSchema>(
  event: TEvent,
  dataSchema: TData,
) {
  return Type.Object(
    {
      data: dataSchema,
      event: Type.Literal(event),
      sequence: SequenceSchema,
      sessionId: OpaqueIdSchema,
      timestamp: TimestampSchema,
      version: Type.Literal(PROTOCOL_VERSION),
    },
    messageOptions,
  )
}

export const ImageClipboardMessageSchema = Type.Union(
  [
    createEnvelope(
      'clipboard.image-offer',
      Type.Object(
        {
          mimeType: Type.Literal('image/png'),
          sha256: Sha256Schema,
          size: Type.Integer({
            maximum: CLIPBOARD_IMAGE_MAX_BYTES,
            minimum: 1,
          }),
          transferId: TransferIdSchema,
        },
        messageOptions,
      ),
    ),
    createEnvelope(
      'clipboard.image-complete',
      Type.Object({ transferId: TransferIdSchema }, messageOptions),
    ),
    createEnvelope(
      'clipboard.image-applied',
      Type.Object({ transferId: TransferIdSchema }, messageOptions),
    ),
    createEnvelope(
      'clipboard.image-error',
      Type.Object(
        {
          code: ErrorCodeSchema,
          transferId: TransferIdSchema,
        },
        messageOptions,
      ),
    ),
  ],
  { $id: 'ImageClipboardMessageV1' },
)

export type ImageClipboardMessage = Static<typeof ImageClipboardMessageSchema>
