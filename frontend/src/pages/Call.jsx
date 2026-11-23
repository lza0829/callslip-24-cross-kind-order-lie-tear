import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'

const fmt = (s) => (s || '').slice(0, 19).replace('T', ' ')

export default function Call() {
  const [wins, setWins] = useState([])
  const [wid, setWid] = useState('')
  const [preview, setPreview] = useState(null)
  const [receipt, setReceipt] = useState(null)
  const [events, setEvents] = useState([])
  const [verify, setVerify] = useState(null)
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')

  const loadPreview = useCallback(async (id) => {
    if (!id) { setPreview(null); return }
    try { setPreview(await api.nextOne(Number(id))) } catch (ex) { setErr(ex.message) }
  }, [])

  const load = async () => {
    try {
      const [w, e] = await Promise.all([api.windows(), api.events()])
      setWins(w)
      setEvents(e)
      const cur = wid || String(w[0]?.id || '')
      if (!wid && w.length) setWid(cur)
      loadPreview(cur)
    } catch (ex) { setErr(ex.message) }
  }
  useEffect(() => { load() }, [])

  const onPickWindow = (id) => {
    setWid(id)
    setReceipt(null)
    loadPreview(id)
  }
  const onCall = async () => {
    setErr(''); setMsg(''); setReceipt(null); setVerify(null)
    try {
      const t = await api.call(Number(wid))
      setReceipt(t.回执)
      setMsg(`已叫 ${t.号牌}（${t.业务种类}），窗口 ${t.窗口}`)
      load()
    } catch (ex) { setErr(ex.message); loadPreview(wid) }
  }
  const onFinish = async (id) => {
    setErr('')
    try { await api.finish(id); load() } catch (ex) { setErr(ex.message) }
  }
  const onSkip = async (id) => {
    setErr('')
    try { await api.skip(id); load() } catch (ex) { setErr(ex.message) }
  }
  const onVerify = async () => {
    setErr('')
    try { setVerify(await api.verify()) } catch (ex) { setErr(ex.message) }
  }
  const win = wins.find(w => String(w.id) === String(wid))
  const current = win?.当前号
  return (
    <div className="page">
      <header className="hero">
        <p className="eyebrow">窗口推进</p>
        <h1>叫号</h1>
        <p className="lede">
          跨种类只认一个口径：窗口可办种类交集内，按取号时刻先进先出，并列以号牌先后破。
          不可按次挑种类；可办种类在「窗口」页勾选维护。跳号会被次序闸整笔回滚。
        </p>
      </header>
      <section className="panel">
        <div className="form row">
          <label>窗口
            <select value={wid} onChange={e => onPickWindow(e.target.value)}>
              {wins.map(w => <option key={w.id} value={w.id}>{w.名称}{w.开放 ? '' : '（关）'}</option>)}
            </select>
          </label>
          <button type="button" onClick={onCall} disabled={!preview?.可叫}>叫下一号</button>
        </div>
        {win && (
          <p className="muted">
            可办种类：{win.可办种类.map(k => k.名称).join('、') || '未配置'}
          </p>
        )}
        {preview && (
          <p className="muted">
            下一号预览：{preview.可叫
              ? `${preview.下一号.号牌}（${preview.下一号.业务种类}，取号 ${fmt(preview.下一号.取号时刻)}，候选 ${preview.候选数}）`
              : preview.拒因}
          </p>
        )}
        {err && <p className="err">{err}</p>}
        {msg && <p className="muted">{msg}</p>}
        {receipt && (
          <div className="slip" style={{ marginTop: 16 }}>
            <div className="no">{receipt.号牌}</div>
            <div className="meta">
              回执 · {receipt.业务种类} · 取号 {fmt(receipt.取号时刻)} · 叫号 {fmt(receipt.叫号时刻)}
            </div>
            <div className="meta">
              {receipt.规则} · 可办 {receipt.可办种类.map(k => k.名称).join('、')} · 候选 {receipt.候选数}
            </div>
            {receipt.次序证据 && (
              <div className="meta">
                次序证据：事件#{receipt.次序证据.事件} · 应叫 {receipt.次序证据.期望号牌} ·
                叫到 {receipt.次序证据.叫到号牌} · {receipt.次序证据.一致 ? '一致' : '冲突'}
              </div>
            )}
          </div>
        )}
        {current && (
          <div className="slip on" style={{ marginTop: 16 }}>
            <div className="no">{current.号牌}</div>
            <div className="meta">{current.业务种类} · {current.状态}</div>
            <div className="actions">
              <button type="button" onClick={() => onFinish(current.id)}>办结</button>
              <button type="button" className="ghost" onClick={() => onSkip(current.id)}>过号</button>
            </div>
          </div>
        )}
      </section>
      <section className="panel">
        <h2>叫号流水</h2>
        <div className="actions">
          <button type="button" className="ghost" onClick={onVerify}>重放校验</button>
        </div>
        {verify && (
          <p className={verify.一致 ? 'muted' : 'err'}>
            重放校验{verify.一致 ? '一致' : `存在 ${verify.冲突数} 处冲突`}：
            检查 {verify.检查数} 个叫号/重叫事件、{verify.状态检查数} 个末态（{verify.规则}）
          </p>
        )}
        {verify && !verify.一致 && (
          <ul className="plain">
            {verify.明细.filter(d => d.结果 === '冲突').map((d, i) => (
              <li key={`${d.事件}-${i}`} className="err">
                {d.事件 != null ? `事件${d.事件} · ` : ''}{d.动作} · {d.号牌} · {d.说明}
              </li>
            ))}
          </ul>
        )}
        <ul className="plain">
          {events.map(e => (
            <li key={e.id}>{fmt(e.时刻)} · {e.窗口} · {e.动作} · {e.号牌} · {e.业务种类}</li>
          ))}
        </ul>
      </section>
    </div>
  )
}
