const STAGE_LABEL = {
  loading_model: 'Загружаю модель распознавания',
  scanning: 'Читаю сообщения',
  downloading: 'Скачиваю медиа',
  transcribing: 'Распознаю речь',
}

function clock(seconds) {
  if (!seconds) return '0:00'
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}

export default function Progress({ data, mode = 'channel', onStop, stopping }) {
  if (!data) return null

  const { current, total, stats, stage, detail } = data
  const pct = total > 0 ? Math.min(100, Math.round((current / total) * 100)) : 0
  const isDm = mode === 'dm'

  // Speech is the slow part, and it moves on a completely different clock from
  // the message counter — one long voice note outweighs hundreds of texts.
  const mediaFound = (stats.voice_count || 0) + (stats.videos_count || 0)
  const voiceSec = stats.voice_seconds || 0
  const doneSec = stats.transcribed_seconds || 0
  const speechPct = voiceSec > 0 ? Math.min(100, Math.round((doneSec / voiceSec) * 100)) : 0

  return (
    <div className="progress-block">
      <h2>
        {isDm ? 'Загрузка переписки: ' : 'Загрузка канала: '}
        <em>{stats.channel_title || stats.channel_username || '...'}</em>
      </h2>

      {isDm && (stage || detail) && (
        <div className={`stage-line stage-line--${stage || 'scanning'}`}>
          <span className="stage-dot" />
          <span className="stage-text">
            {detail || STAGE_LABEL[stage] || 'Работаю'}
          </span>
        </div>
      )}

      <div className="progress-bar-wrap">
        <div className="progress-bar" style={{ width: `${pct}%` }} />
      </div>
      <p className="progress-label">
        {current} / {total > 0 ? total : '?'} {isDm ? 'сообщений' : 'постов'} ({pct}%)
      </p>

      {isDm && mediaFound > 0 && (
        <div className="speech-block">
          <div className="speech-head">
            <span className="speech-title">Распознавание речи</span>
            <span className="speech-count">
              {stats.transcribed_count} из {mediaFound}
            </span>
          </div>

          <div className="progress-bar-wrap progress-bar-wrap--speech">
            <div className="progress-bar progress-bar--speech" style={{ width: `${speechPct}%` }} />
          </div>

          <p className="progress-label">
            {clock(doneSec)} из {clock(voiceSec)} звука ({speechPct}%)
          </p>

          <div className="speech-split">
            <span>Голосовых: <strong>{stats.voice_count || 0}</strong></span>
            <span>Кружков: <strong>{stats.videos_count || 0}</strong></span>
            {stats.queued_media > 0 && (
              <span className="queue-badge" title="Скачано заранее и ждёт распознавания">
                в очереди: <strong>{stats.queued_media}</strong>
              </span>
            )}
          </div>
        </div>
      )}

      <div className="stat-grid">
        <StatItem label="Сохранено" value={stats.saved_posts} />
        <StatItem label="Пропущено" value={stats.skipped_posts} />
        {isDm ? (
          <>
            <StatItem label="Голосовые" value={stats.voice_count} />
            <StatItem label="Кружки" value={stats.videos_count} />
            <StatItem label="Распознано" value={stats.transcribed_count} />
          </>
        ) : (
          <>
            <StatItem label="Фото" value={stats.photos_count} />
            <StatItem label="Видео" value={stats.videos_count} />
            <StatItem label="Голос" value={stats.voice_count} />
            <StatItem label="Файлы" value={stats.files_count} />
            <StatItem label="Ссылки" value={stats.links_count} />
          </>
        )}
      </div>

      {onStop && (
        <div className="stop-row">
          <button className="stop-btn" onClick={onStop} disabled={stopping}>
            {stopping ? 'Останавливаю...' : 'Остановить скачивание'}
          </button>
          <span className="stop-note">
            Скачанные данные {isDm ? 'этого чата' : 'этого канала'} будут удалены
          </span>
        </div>
      )}
    </div>
  )
}

function StatItem({ label, value }) {
  return (
    <div className="stat-item">
      <span className="stat-value">{value}</span>
      <span className="stat-label">{label}</span>
    </div>
  )
}
