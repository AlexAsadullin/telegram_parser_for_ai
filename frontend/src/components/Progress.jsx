export default function Progress({ data, mode = 'channel' }) {
  if (!data) return null

  const { current, total, stats } = data
  const pct = total > 0 ? Math.min(100, Math.round((current / total) * 100)) : 0
  const isDm = mode === 'dm'

  return (
    <div className="progress-block">
      <h2>
        {isDm ? 'Загрузка переписки: ' : 'Загрузка канала: '}
        <em>{stats.channel_title || stats.channel_username || '...'}</em>
      </h2>

      <div className="progress-bar-wrap">
        <div className="progress-bar" style={{ width: `${pct}%` }} />
      </div>
      <p className="progress-label">
        {current} / {total > 0 ? total : '?'} {isDm ? 'сообщений' : 'постов'} ({pct}%)
      </p>

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

      {isDm && stats.voice_count + stats.videos_count > 0 && (
        <p className="progress-note">
          Распознавание речи идёт локально — на голосовых и кружках прогресс замедляется.
        </p>
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
