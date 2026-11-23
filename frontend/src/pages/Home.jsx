import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Home() {
  const [o, setO] = useState(null)
  const [err, setErr] = useState('')
  useEffect(() => { api.overview().then(setO).catch(e => setErr(e.message)) }, [])
  if (err) return <p className="err">{err}</p>
  if (!o) return <p className="muted">加载总览…</p>
  const audit = o.次序审计
  const cards = [
    ['等候', o.等候], ['叫号中', o.叫号中], ['办理中', o.办理中],
    ['过号', o.过号], ['已办结', o.已办结], ['已作废', o.已作废],
    ['开放窗口', o.开放窗口],
  ]
  return (
    <div className="page">
      <header className="hero">
        <p className="eyebrow">网点队列物化</p>
        <h1>叫号条</h1>
        <p className="lede">
          营业日 {o.营业日} · {o.已封口 ? '已封口' : '营业中'}。取号入队，窗口叫号推进，过号可回收重叫，当日封口后禁写。
        </p>
      </header>
      {audit && !audit.一致 && (
        <p className="err">
          次序审计冲突 {audit.冲突数} 处：跨种类叫号次序未闭合，总览计数与墙/序号页同源，不得视为正常办理。
        </p>
      )}
      <div className="stat-grid">
        {cards.map(([k, v]) => (
          <div className="stat" key={k}><span>{k}</span><strong>{v}</strong></div>
        ))}
      </div>
      <section className="panel">
        <h2>窗口现况</h2>
        <ul className="plain">
          {o.窗口.map(w => (
            <li key={w.id}>
              {w.名称} · {w.开放 ? '开放' : '关闭'} · {w.有在办 ? '有在办号' : '空闲'}
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
