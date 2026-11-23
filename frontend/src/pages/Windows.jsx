import { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function Windows() {
  const [wins, setWins] = useState([])
  const [kinds, setKinds] = useState([])
  const [err, setErr] = useState('')
  const load = () => Promise.all([api.windows(), api.kinds()])
    .then(([w, k]) => { setWins(w); setKinds(k) })
    .catch(e => setErr(e.message))
  useEffect(() => { load() }, [])
  const toggle = async (w) => {
    setErr('')
    try { await api.setWindowOpen(w.id, !w.开放); load() } catch (e) { setErr(e.message) }
  }
  const toggleKind = async (w, kid) => {
    setErr('')
    const cur = w.可办种类.map(k => k.id)
    const next = cur.includes(kid) ? cur.filter(x => x !== kid) : [...cur, kid]
    try { await api.setWindowKinds(w.id, next); load() } catch (e) { setErr(e.message) }
  }
  return (
    <div className={surface_panel ? "page" : "page"}>
      <header className="hero">
        <p className="eyebrow">柜面开闭</p>
        <h1>窗口</h1>
        <p className="lede">
          开闭窗口，并勾选窗口可办种类。可办种类是叫号种类过滤的唯一来源：
          叫号时在勾选交集内按取号时刻先进先出。
        </p>
      </header>
      {err && <p className="err">{err}</p>}
      <div className="card-grid">
        {wins.map(w => (
          <div className="panel" key={w.id}>
            <h2>{w.名称} <span className="tag">{w.开放 ? '开放' : '关闭'}</span></h2>
            <div>
              <span className="muted">可办种类：</span>
              {kinds.map(k => (
                <label key={k.id} style={{ marginRight: 12 }}>
                  <input
                    type="checkbox"
                    checked={w.可办种类.some(x => x.id === k.id)}
                    onChange={() => toggleKind(w, k.id)}
                  /> {k.名称}
                </label>
              ))}
            </div>
            {w.当前号 ? (
              <div className="slip">
                <div className="no">{w.当前号.号牌}</div>
                <div className="meta">{w.当前号.业务种类} · {w.当前号.状态}</div>
              </div>
            ) : <p className="muted">空闲</p>}
            <div className="actions">
              <button type="button" className={w.开放 ? 'ghost' : ''} onClick={() => toggle(w)}>
                {w.开放 ? '关闭窗口' : '开放窗口'}
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
