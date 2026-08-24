import { useEffect, useState } from 'react'
import axios from 'axios'

const STATUS_LABEL = {
  completed: 'Завершён',
  failed: 'Ошибка',
  in_progress: 'В процессе',
}

// A user-stopped run is recorded as 'failed' (the SQLite status column has a
// CHECK constraint that predates the stop button), but it is not an error and
// should not be shown as one.
const STOPPED_PREFIX = 'Остановлено пользователем'

function statusOf(row) {
  if (row.status === 'failed' && (row.error_message || '').startsWith(STOPPED_PREFIX)) {
    return { label: 'Остановлен', className: 'status-stopped' }
  }
  return {
    label: STATUS_LABEL[row.status] ?? row.status,
    className: `status-${row.status}`,
  }
}

export default function History({ refreshKey }) {
  const [rows, setRows] = useState([])
  // Loading is derived: the fetch for the current refreshKey hasn't settled yet.
  const [loadedKey, setLoadedKey] = useState(null)
  const loading = loadedKey !== refreshKey

  useEffect(() => {
    let cancelled = false
    axios.get('/api/history')
      .then((res) => { if (!cancelled) setRows(res.data) })
      .catch(() => {})
      .finally(() => { if (!cancelled) setLoadedKey(refreshKey) })
    return () => { cancelled = true }
  }, [refreshKey])

  if (loading) return <p className="history-loading">Загрузка истории...</p>
  if (!rows.length) return <p className="history-empty">История пуста</p>

  return (
    <div className="history-block">
      <h2>История запросов</h2>
      <div className="history-scroll">
        <table className="history-table">
          <thead>
            <tr>
              <th>Дата</th>
              <th>Канал</th>
              <th>Статус</th>
              <th>Постов</th>
              <th>Сохранено</th>
              <th>Пропущено</th>
              <th>Фото</th>
              <th>Видео</th>
              <th>Голос</th>
              <th>Файлы</th>
              <th>Ссылки</th>
              <th>Ошибка</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{r.created_at}</td>
                <td>{r.channel_title || r.channel_username || r.channel_id || '—'}</td>
                <td className={statusOf(r).className}>{statusOf(r).label}</td>
                <td>{r.total_posts}</td>
                <td>{r.saved_posts}</td>
                <td>{r.skipped_posts}</td>
                <td>{r.photos_count}</td>
                <td>{r.videos_count}</td>
                <td>{r.voice_count}</td>
                <td>{r.files_count}</td>
                <td>{r.links_count}</td>
                <td className="error-cell" title={r.error_message || ''}>
                  {r.error_message
                    ? (r.error_message.length > 60
                        ? r.error_message.slice(0, 60) + '…'
                        : r.error_message)
                    : ''}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
