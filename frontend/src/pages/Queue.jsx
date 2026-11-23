import { useEffect, useState } from 'react'
import { api } from '../api'

const fmt = (s) => (s || '').slice(0, 19).replace('T', ' ')

export default function Queue() {
  const [rows, setRows] = useState([])
  const [err, setErr] = useState('')

  async function load() {
    try {
      setErr('')
      setRows(await api.tickets('等候'))
    } catch (e) {
      setErr(e.message)
    }
  }

  useEffect(() => { load() }, [])

  const conflict = rows.some(r => r.次序审计一致 === false)

  return (
    <div className="page">
      <header className="hero">
        <p className="eyebrow">等候序号</p>
        <h1>号牌序号</h1>
        <p className="lede">
          当日等候号按全局取号时刻排列（并列以号牌id破），与叫号入口、等候墙同一谓词。
          「可叫」只表示该号此刻落在某个开放窗口的可叫集合内，与墙高亮、叫号流水同源。
        </p>
      </header>
      {err && <p className="err">{err}</p>}
      {conflict && <p className="err">次序审计冲突：序号页不得把跳号显示为正常可叫。</p>}
      <section className="panel">
        <div className="actions">
          <button type="button" className="ghost" onClick={load}>刷新</button>
        </div>
        <ul className="plain">
          {rows.map((t) => (
            <li key={t.id}>
              <span className="tag">#{t.次序}</span>
              <strong>{t.号牌}</strong> · {t.业务种类} · 取号 {fmt(t.取号时刻)}
              {' · '}
              {t.可叫 ? `可叫（${t.可叫窗口.join('、')}）` : '暂无可叫窗口'}
            </li>
          ))}
        </ul>
        {!rows.length && <p className="muted">暂无等候号</p>}
      </section>
    </div>
  )
}
