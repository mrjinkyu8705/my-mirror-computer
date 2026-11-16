import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { ConnectionTimingDetails } from '../src/ConnectionTimingDetails'
import { ConnectionTimingProbe } from '../src/connectionTiming'
afterEach(cleanup)
it('labels independent observations and never invents a browser delay from RTT subtraction', () => {
  const probe = new ConnectionTimingProbe()
  const timing = probe.sample(new Map() as unknown as RTCStatsReport, 0, 1)
  render(<ConnectionTimingDetails timing={timing} />)
  const panel = screen.getByTestId('connection-timing')
  expect(panel.textContent).toContain('ICE 경로 RTT 측정 전 (미제공)')
  expect(panel.textContent).toContain('브라우저 타이머 지연')
  expect(panel.textContent).toContain('미지원')
  expect(panel.title).toContain('두 RTT의 차이가 브라우저 지연은 아닙니다')
})
it('labels stale measurements and excludes hidden browser timing', () => {
  const timing = new ConnectionTimingProbe().sample(new Map() as unknown as RTCStatsReport, 0, 1)
  render(<ConnectionTimingDetails timing={{ ...timing, iceRttMs: 12, iceState: 'stale', iceAgeMs: 20000,
    appRttMs: 550, appAgeMs: 6000, visibility: 'hidden', longTasksSupported: true }} />)
  expect(screen.getByTestId('connection-timing').textContent).toContain('오래된 값')
  expect(screen.getByTestId('connection-timing').textContent).toContain('백그라운드 · 측정 제외')
})
