import { describe, expect, it } from 'vitest'

import {
  CONTROL_MAX_MISSED_PONGS,
  controlChannelNeedsRecovery,
} from '../src/controlChannelHealth'

describe('controlChannelNeedsRecovery', () => {
  it('recovers immediately when the DataChannel is no longer open', () => {
    expect(controlChannelNeedsRecovery('closed', 0)).toBe(true)
    expect(controlChannelNeedsRecovery('closing', 0)).toBe(true)
  })

  it('tolerates short loss but recovers after consecutive missed pongs', () => {
    expect(
      controlChannelNeedsRecovery('open', CONTROL_MAX_MISSED_PONGS - 1),
    ).toBe(false)
    expect(
      controlChannelNeedsRecovery('open', CONTROL_MAX_MISSED_PONGS),
    ).toBe(true)
  })
})
