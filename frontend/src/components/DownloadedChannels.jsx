import { useEffect, useState } from 'react'
import axios from 'axios'

export default function DownloadedChannels({ onBack }) {
  const [channels, setChannels] = useState([])
  const [loading, setLoading] = useState(true)
  const [exporting, setExporting] = useState({})  // channel_id → 'plain'|'transcribe'|null

  useEffect(() => {
    axios.get('/api/channels')
      .then((res) => setChannels(res.data))
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  const handleExport = async (channelId, transcribe) => {
    const key = channelId
    setExporting((prev) => ({ ...prev, [key]: transcribe ? 'transcribe' : 'plain' }))

    try {
      const url = `/api/channels/${channelId}/export?transcribe=${transcribe}`
      // trigger browser file download
      const res = await axios.get(url, { responseType: 'blob' })
      const disposition = res.headers['content-disposition'] || ''
      const match = disposition.match(/filename="([^"]+)"/)
      const filename = match ? match[1] : `${channelId}_export.txt`

      const blobUrl = URL.createObjectURL(res.data)
      const a = document.createElement('a')
      a.href = blobUrl
      a.download = filename
      a.click()
      URL.revokeObjectURL(blobUrl)
    } catch {
      alert('Ошибка при экспорте')
    } finally {
      setExporting((prev) => ({ ...prev, [key]: null }))
    }
  }

  return (
    <div className="channels-page">
      <div className="channels-header">
        <button className="back-btn" onClick={onBack}>← Назад</button>
        <h2>Скачанные каналы</h2>
      </div>

      {loading && <p className="history-loading">Загрузка...</p>}

      {!loading && channels.length === 0 && (
        <p className="history-empty">Нет скачанных каналов. Введите ссылку на канал на главном экране.</p>
      )}

      {!loading && channels.length > 0 && (
        <div className="channels-list">
          {channels.map((ch) => {
            const busy = exporting[ch.channel_id]
            return (
              <div key={ch.channel_id} className="channel-card">
                <div className="channel-card-info">
                  <div className="channel-card-title">
                    {ch.title || ch.username || ch.channel_id}
                    {ch.username && (
                      <span className="channel-card-username"> @{ch.username}</span>
                    )}
                  </div>
                  <div className="channel-card-meta">
                    {ch.subscribers != null && (
                      <span>{Number(ch.subscribers).toLocaleString('ru')} подп.</span>
                    )}
                    <span>{ch.posts_count} постов</span>
                    {ch.voice_count > 0 && (
                      <span className="voice-badge">{ch.voice_count} голос.</span>
                    )}
                    <span className="ch-type">{ch.type === 'public' ? 'публичный' : 'приватный'}</span>
                  </div>
                </div>

                <div className="channel-card-actions">
                  <button
                    className="export-btn"
                    disabled={!!busy}
                    onClick={() => handleExport(ch.channel_id, false)}
                  >
                    {busy === 'plain' ? 'Экспорт...' : 'Выгрузить текст'}
                  </button>

                  {ch.voice_count > 0 && (
                    <button
                      className="export-btn export-btn--transcribe"
                      disabled={!!busy}
                      onClick={() => handleExport(ch.channel_id, true)}
                      title="Требуется Telegram Premium"
                    >
                      {busy === 'transcribe' ? 'Транскрибация...' : 'Текст + транскрипция'}
                    </button>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
