import { useEffect, useState } from 'react'
import { api } from '../api'

const fmt = (s) => (s || '').slice(0, 19).replace('T', ' ')

export default function Wall() {
  const [wall, setWall] = useState(null)
  const [err, setErr] = useState('')
  useEffect(() => { api.wall().then(setWall).catch(e => setErr(e.message)) }, [])
  if (err) return <p className="err">{err}</p>
  if (!wall) return <p className="muted">加载等候墙…</p>
  const audit = wall.次序审计
  return (
    <div className="page">
      <header className="hero">
        <p className="eyebrow">位次展示</p>
        <h1>等候墙</h1>
        <p className="lede">营业日 {wall.营业日}。高亮与「窗口下一号」由同一跨种类可叫集合谓词算出，并与叫号流水同命运。{wall.选号规则}</p>
      </header>
      {audit && !audit.一致 && (
        <p className="err">次序审计冲突 {audit.冲突数} 处：墙高亮与叫号流水不一致，跳号不得显示为正常办理。</p>
      )}
      {wall.窗口下一号?.length > 0 && (
        <section className="panel">
          <h2>窗口下一号（与叫号入口同谓词）</h2>
          <div className="card-grid">
            {wall.窗口下一号.map(n => (
              <div className="slip" key={n.窗口编号}>
                <div className="no">{n.可叫 ? n.下一号.号牌 : '—'}</div>
                <div className="meta">
                  {n.窗口} · {n.可叫
                    ? `${n.下一号.业务种类} · 取号 ${fmt(n.下一号.取号时刻)} · 候选 ${n.候选数} · 谓词序 #${n.下一号.id}`
                    : n.拒因}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}
      <section className="panel">
        <h2>跨种类叫号次序（取号时刻升序）</h2>
        {wall.叫号次序?.length ? (
          <ul className="plain">
            {wall.叫号次序.map(t => (
              <li key={t.id}>
                <span className="tag">#{t.次序}</span>
                {t.号牌} · {t.业务种类} · 取号 {fmt(t.取号时刻)} ·{' '}
                {t.高亮 ? `可叫（${t.可叫窗口.join('、')}）` : '暂无可叫窗口'}
              </li>
            ))}
          </ul>
        ) : <p className="muted">暂无等候号</p>}
      </section>
      {wall.在办?.length > 0 && (
        <section className="panel">
          <h2>在办</h2>
          <div className="card-grid">
            {wall.在办.map(t => (
              <div className={t.高亮 ? 'slip on' : 'slip'} key={t.id}>
                <div className="no">{t.号牌}</div>
                <div className="meta">
                  {t.窗口} · {t.业务种类} · {t.状态}
                  {t.叫号事件 ? ` · 叫号事件#${t.叫号事件}` : ''}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}
      {wall.最后叫号 && (
        <p className="muted">
          最后叫号：事件#{wall.最后叫号.事件} · {wall.最后叫号.号牌} · {fmt(wall.最后叫号.时刻)}
        </p>
      )}
      {wall.墙面.map(block => (
        <section className="panel" key={block.业务种类}>
          <h2>{block.业务种类} · 等候 {block.等候数} · 过号 {block.过号数}</h2>
          <div className="card-grid">
            {block.队列.length === 0 && <p className="muted">暂无号牌</p>}
            {block.队列.map(t => (
              <div className={t.高亮 ? 'slip on' : 'slip'} key={t.id}>
                <div className="no">{t.号牌}</div>
                <div className="meta">
                  {t.状态} · 位次 {t.位次}
                  {t.状态 === '等候' && t.次序 ? ` · 全局序 #${t.次序}` : ''}
                  {t.高亮 ? ` · 可叫 ${t.可叫窗口.join('、')}` : ''}
                </div>
              </div>
            ))}
          </div>
        </section>
      ))}
    </div>
  )
}
