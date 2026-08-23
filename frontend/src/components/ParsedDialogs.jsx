import { useEffect, useState } from 'react'
import axios from 'axios'

export default function ParsedDialogs({ onBack }) {
  const [dialogs, setDialogs] = useState([])
  const [loading, setLoading] = useState(true)
  const [exporting, setExporting] = useState({})

  useEffect(() => {
    let cancelled = false
    axios.get('/api/dms')
      .then((res) => { if (!cancelled) setDialogs(res.data) })
      .catch(() => {})
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [])

  const handleExport = async (dialogId) => {
    setExporting((prev) => ({ ...prev, [dialogId]: true }))
    try {
      const res = await axios.get(`/api/dms/${dialogId}/export`, { responseType: 'blob' })
      const disposition = res.headers['content-disposition'] || ''
      const match = disposition.match(/filename="([^"]+)"/)
      const filename = match ? match[1] : `dm_${dialogId}_export.txt`

      const blobUrl = URL.createObjectURL(res.data)
      const a = document.createElement('a')
      a.href = blobUrl
      a.download = filename
      a.click()
      URL.revokeObjectURL(blobUrl)
    } catch {
      alert('Ошибка при экспорте')
    } finally {
      setExporting((prev) => ({ ...prev, [dialogId]: false }))
    }
  }

  return (
    <div className="channels-page">
      <div className="channels-header">
        <button className="back-btn" onClick={onBack}>← Назад</button>
        <h2>Скачанные переписки</h2>
      </div>

      {loading && <p className="history-loading">Загрузка...</p>}

      {!loading && dialogs.length === 0 && (
        <p className="history-empty">
          Нет скачанных переписок. Выберите чат на главном экране во вкладке «Личные сообщения».
        </p>
      )}

      {!loading && dialogs.length > 0 && (
        <div className="channels-list">
          {dialogs.map((d) => (
            <div key={d.dialog_id} className="channel-card">
              <div className="channel-card-info">
                <div className="channel-card-title">
                  {d.title || d.dialog_id}
                  {d.username && <span className="channel-card-username"> @{d.username}</span>}
                </div>
                <div className="channel-card-meta">
                  <span>{d.messages_count} сообщений</span>
                  {d.transcribed_count > 0 && (
                    <span className="voice-badge">{d.transcribed_count} распознано</span>
                  )}
                </div>
              </div>

              <div className="channel-card-actions">
                <button
                  className="export-btn"
                  disabled={!!exporting[d.dialog_id]}
                  onClick={() => handleExport(d.dialog_id)}
                >
                  {exporting[d.dialog_id] ? 'Экспорт...' : 'Выгрузить текст'}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
