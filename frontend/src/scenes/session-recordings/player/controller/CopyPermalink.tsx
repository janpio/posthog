import { useValues } from 'kea'

import { LemonButton } from '@posthog/lemon-ui'

import { IconLink } from 'lib/lemon-ui/icons'
import { copyToClipboard } from 'lib/utils/copyToClipboard'
import { cn } from 'lib/utils/css-classes'
import { sessionRecordingPlayerLogic } from 'scenes/session-recordings/player/sessionRecordingPlayerLogic'
import { urls } from 'scenes/urls'

export function CopyPermalink({ className, ...props }: { className?: string; 'data-attr'?: string }): JSX.Element {
    const { sessionRecordingId, logicProps } = useValues(sessionRecordingPlayerLogic)

    const copyPermalink = (): void => {
        if (!sessionRecordingId) {
            return
        }
        // NOTE: We pull the time at call time as otherwise it would trigger re-renders on every player tick
        const playerTime = sessionRecordingPlayerLogic.findMounted(logicProps)?.values.currentPlayerTime || 0
        const seconds = Math.floor(playerTime / 1000)
        const path = urls.replaySingle(sessionRecordingId)
        const separator = path.includes('?') ? '&' : '?'
        const url = `${window.location.origin}${path}${seconds ? `${separator}t=${seconds}` : ''}`
        void copyToClipboard(url, 'permalink')
    }

    return (
        <LemonButton
            size="xsmall"
            onClick={(e) => {
                e.stopPropagation()
                copyPermalink()
            }}
            tooltip="Copy a link to this point in the recording"
            icon={<IconLink className={cn('text-xl', className)} />}
            tooltipPlacement="top"
            {...props}
        />
    )
}
