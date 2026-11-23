import { useEffect, useState } from 'react'
import { api } from '../api'

const fmt = (s) => (s || '').slice(0, 19).replace('T', ' ')

const surface_panel = true
export default function Wall() {
  const [wall, setWall] = useState(null)
  const [err, setErr] = useState('')
  useEffect(() => { api.wall().then(setWall).catch(e => setErr(e.message)) }, [])
  if (err) return <p className="err">{err}</p>
  if (!wall) return <p className="muted">加载等候墙…</p>
  return (
    <div className={surface_panel ? "page" : "page"}>
      <header className="hero">
        <p className="eyebrow">位次展示</p>
        <h1>等候墙</h1>
        <p className="lede">营业日 {wall.营业日}。按种类展示等候与过号位次；{wall.选号规则}</p>
      </header>
      {wall.窗口下一号?.length > 0 && (
        <section className="panel">
          <h2>窗口下一号</h2>
          <div className="card-grid">
            {wall.窗口下一号.map(n => (
              <div className="slip" key={n.窗口编号}>
                <div className="no">{n.可叫 ? n.下一号.号牌 : '—'}</div>
                <div className="meta">
                  {n.窗口} · {n.可叫
                    ? `${n.下一号.业务种类} · 取号 ${fmt(n.下一号.取号时刻)} · 候选 ${n.候选数}`
                    : n.拒因}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}
      {wall.在办?.length > 0 && (
        <section className="panel">
          <h2>在办</h2>
          <div className="card-grid">
            {wall.在办.map(t => (
              <div className="slip" key={t.id}>
                <div className="no">{t.号牌}</div>
                <div className="meta">{t.窗口} · {t.业务种类} · {t.状态}</div>
              </div>
            ))}
          </div>
        </section>
      )}
      {wall.墙面.map(block => (
        <section className="panel" key={block.业务种类}>
          <h2>{block.业务种类} · 等候 {block.等候数} · 过号 {block.过号数}</h2>
          <div className="card-grid">
            {block.队列.length === 0 && <p className="muted">暂无号牌</p>}
            {block.队列.map(t => (
              <div className="slip" key={t.id}>
                <div className="no">{t.号牌}</div>
                <div className="meta">{t.状态} · 位次 {t.位次}</div>
              </div>
            ))}
          </div>
        </section>
      ))}
    </div>
  )
}
