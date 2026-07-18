import { useState } from 'react'
import { downloadUrl } from '../api'
import { formatBytes } from '../formatBytes'
import type { ArtifactResponse, JobStatusResponse } from '../types'

export type ArtifactKind = 'video' | 'image' | 'other'

export function classifyArtifact(contentType: string | null): ArtifactKind {
  if (!contentType) return 'other'
  if (contentType.startsWith('video/')) return 'video'
  if (contentType.startsWith('image/')) return 'image'
  return 'other'
}

interface ArtifactCardProps {
  jobId: string
  shotIndex: number
  artifact: ArtifactResponse
}

function ArtifactCard({ jobId, shotIndex, artifact }: ArtifactCardProps) {
  const [previewFailed, setPreviewFailed] = useState(false)
  const kind = classifyArtifact(artifact.content_type)
  const url = downloadUrl(jobId, shotIndex, artifact.filename)

  return (
    <li className="job-artifacts__card">
      {!previewFailed && kind === 'video' && (
        <video controls className="job-artifacts__preview" src={url} onError={() => setPreviewFailed(true)} />
      )}
      {!previewFailed && kind === 'image' && (
        <img
          className="job-artifacts__preview"
          src={url}
          alt={artifact.filename}
          onError={() => setPreviewFailed(true)}
        />
      )}
      <div className="job-artifacts__meta">
        <span className="job-artifacts__filename">{artifact.filename}</span>
        <span className="job-artifacts__size">{formatBytes(artifact.size_bytes)}</span>
        <a className="job-artifacts__download" href={url} target="_blank" rel="noreferrer">
          Download
        </a>
      </div>
    </li>
  )
}

interface Props {
  job: JobStatusResponse
}

export function JobArtifacts({ job }: Props) {
  const artifacts = (job.shots ?? []).flatMap((shot) =>
    (shot.files ?? []).map((artifact) => ({ shotIndex: shot.shot_index, artifact })),
  )

  if (artifacts.length === 0) return <p>No output artifacts yet.</p>

  return (
    <ul className="job-artifacts">
      {artifacts.map(({ shotIndex, artifact }) => (
        <ArtifactCard key={`${shotIndex}-${artifact.filename}`} jobId={job.id} shotIndex={shotIndex} artifact={artifact} />
      ))}
    </ul>
  )
}
