import type { ConnectionTiming } from './connectionTiming'

const ms = (value: number | null) => value === null ? '측정 전' : `${value.toFixed(1)} ms`
const iceStates = { unavailable: '미제공', unverified: '갱신 시각 확인 전', recent: '최근 갱신 관측', stale: '오래된 값' }

export function ConnectionTimingDetails({ timing }: { readonly timing: ConnectionTiming }) {
  return <div data-testid="connection-timing" title="ICE는 브라우저 네이티브 STUN 왕복 측정입니다. 앱 RTT에는 브라우저·호스트 처리와 SCTP 대기도 포함됩니다. 두 RTT의 차이가 브라우저 지연은 아닙니다.">
    <span>ICE 경로 RTT {ms(timing.iceRttMs)} ({iceStates[timing.iceState]}
      {timing.iceAgeMs === null ? '' : ` · 갱신 관측 후 ${(timing.iceAgeMs / 1000).toFixed(1)}초`}) · </span>
    <span>앱 응답 RTT {ms(timing.appRttMs)}
      {timing.appAgeMs === null ? '' : ` (${(timing.appAgeMs / 1000).toFixed(1)}초 전${timing.appAgeMs > 5000 ? ' · 오래된 값' : ''})`} · </span>
    <span>브라우저 타이머 지연(5초 최대) {timing.visibility === 'hidden' ? '백그라운드 · 측정 제외' : ms(timing.loopDelayMaxMs)} · </span>
    <span>긴 작업(≥50 ms, 5초) {!timing.longTasksSupported ? '미지원' : timing.visibility === 'hidden' ? '측정 제외' : `${timing.longTaskCount ?? '—'}회 / 최대 ${ms(timing.longTaskMaxMs)}`} · </span>
    <span>통계 조회 대기 {ms(timing.statsAwaitMs)}</span>
  </div>
}
