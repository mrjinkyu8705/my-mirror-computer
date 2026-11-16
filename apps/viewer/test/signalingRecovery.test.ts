import { describe, expect, it } from 'vitest'

import {
  SIGNALING_RECOVERY_INITIAL_DELAY_MS,
  SIGNALING_RECOVERY_MAX_DELAY_MS,
  canRecoverSignalingWithoutPeerRestart,
  nextSignalingRecoveryDelay,
} from '../src/signalingRecovery'

describe('signaling recovery policy', () => {
  it('preserves an active peer while signaling reconnects', () => {
    expect(
      canRecoverSignalingWithoutPeerRestart(
        'control-active',
        'connected',
        'open',
      ),
    ).toBe(true)
    expect(
      canRecoverSignalingWithoutPeerRestart(
        'view-active',
        'disconnected',
        'open',
      ),
    ).toBe(true)
  })

  it('uses the full reconnect path during negotiation or peer failure', () => {
    expect(
      canRecoverSignalingWithoutPeerRestart(
        'negotiating',
        'connected',
        'open',
      ),
    ).toBe(false)
    expect(
      canRecoverSignalingWithoutPeerRestart(
        'control-active',
        'failed',
        'open',
      ),
    ).toBe(false)
    expect(
      canRecoverSignalingWithoutPeerRestart(
        'control-active',
        'connected',
        'closed',
      ),
    ).toBe(false)
  })

  it('backs off independently without exceeding thirty seconds', () => {
    expect(nextSignalingRecoveryDelay(SIGNALING_RECOVERY_INITIAL_DELAY_MS)).toBe(
      4_000,
    )
    expect(nextSignalingRecoveryDelay(16_000)).toBe(
      SIGNALING_RECOVERY_MAX_DELAY_MS,
    )
    expect(nextSignalingRecoveryDelay(SIGNALING_RECOVERY_MAX_DELAY_MS)).toBe(
      SIGNALING_RECOVERY_MAX_DELAY_MS,
    )
  })
})
