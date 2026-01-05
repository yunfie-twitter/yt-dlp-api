/**
 * yt-dlp Web Client
 * Modern application with VueUse, axios, dayjs, DOMPurify
 * All libraries loaded via CDN - no Node.js required
 */

const { createApp, ref, computed, onMounted, watch } = Vue
const { 
    useStorage, 
    useWebSocket, 
    useDark, 
    useToggle,
    useClipboard,
    useDebounceFn,
    useThrottleFn 
} = VueUse

createApp({
    setup() {
        // API client with axios
        const api = axios.create({
            timeout: 15000,
            headers: { 'Content-Type': 'application/json' }
        })

        // Add response interceptor for error handling
        api.interceptors.response.use(
            response => response,
            error => {
                console.error('API Error:', error)
                const message = error.response?.data?.detail || error.message || 'リクエストに失敗しました'
                return Promise.reject(new Error(message))
            }
        )

        // Reactive state
        const url = ref('')
        const info = ref(null)
        const loading = ref(false)
        const downloading = ref(false)
        const error = ref(null)
        const selectedQuality = ref('')
        const currentPage = ref('home')
        const showDeleteModal = ref(false)
        const showSettingsModal = ref(false)
        const downloadProgress = ref(null)
        const currentTaskId = ref(null)

        // VueUse: LocalStorage for history (auto-sync)
        const history = useStorage('downloadHistory', [])

        // VueUse: Dark mode (auto-sync with system preference)
        const isDarkMode = useDark({
            selector: 'body',
            attribute: 'class',
            valueDark: 'dark',
            valueLight: 'light'
        })
        const toggleDark = useToggle(isDarkMode)

        // VueUse: Clipboard
        const { copy, copied, isSupported: clipboardSupported } = useClipboard()

        // Computed properties
        const availableQualities = computed(() => {
            if (!info.value || !info.value.formats) return []
            const qualities = new Set()
            info.value.formats.forEach(format => {
                if (format.height && format.vcodec !== 'none') {
                    qualities.add(format.height)
                }
            })
            return Array.from(qualities).filter(q => [1080, 720, 480].includes(q)).sort((a, b) => b - a)
        })

        // Sanitize user-generated content
        const sanitize = (dirty) => {
            if (!dirty) return ''
            return DOMPurify.sanitize(dirty, { 
                ALLOWED_TAGS: [],
                ALLOWED_ATTR: [] 
            })
        }

        // Format video info safely
        const safeInfo = computed(() => {
            if (!info.value) return null
            return {
                ...info.value,
                title: sanitize(info.value.title),
                uploader: sanitize(info.value.uploader),
                thumbnail: info.value.thumbnail // URL is safe
            }
        })

        // History management with deduplication
        const saveToHistory = (videoInfo) => {
            const historyItem = {
                url: url.value,
                title: sanitize(videoInfo.title),
                thumbnail: videoInfo.thumbnail,
                uploader: sanitize(videoInfo.uploader),
                timestamp: Date.now()
            }
            
            // Remove duplicates by URL
            const filtered = history.value.filter(item => item.url !== url.value)
            history.value = [historyItem, ...filtered].slice(0, 20)
        }

        const confirmClearHistory = () => {
            showDeleteModal.value = true
        }

        const clearHistory = () => {
            history.value = []
            showDeleteModal.value = false
        }

        const deleteHistoryItem = (index) => {
            history.value.splice(index, 1)
        }

        const loadFromHistory = (item) => {
            url.value = item.url
            currentPage.value = 'home'
            fetchInfo()
        }

        // Format timestamp with dayjs
        const formatTimestamp = (timestamp) => {
            return dayjs(timestamp).format('YYYY/MM/DD HH:mm')
        }

        const formatRelativeTime = (timestamp) => {
            return dayjs(timestamp).fromNow()
        }

        // Navigation
        const navigateToPage = (page) => {
            currentPage.value = page
        }

        // Theme management (VueUse handles everything)
        const toggleTheme = (event) => {
            isDarkMode.value = event.target.checked
        }

        const toggleThemeManual = () => {
            toggleDark()
        }

        // Clipboard with VueUse
        const pasteFromClipboard = async () => {
            try {
                const text = await navigator.clipboard.readText()
                url.value = text
            } catch (e) {
                error.value = 'クリップボードからの読み取りに失敗しました'
            }
        }

        // Video info with debounce
        const fetchInfoImmediate = async () => {
            if (!url.value) return
            loading.value = true
            error.value = null
            info.value = null
            
            try {
                const response = await api.post('/info', { url: url.value })
                info.value = response.data
            } catch (e) {
                error.value = e.message
            } finally {
                loading.value = false
            }
        }

        // Debounced version for auto-fetch
        const fetchInfo = useDebounceFn(fetchInfoImmediate, 300)

        const formatDuration = (seconds) => {
            if (!seconds) return 'Live'
            const duration = dayjs.duration(seconds, 'seconds')
            const h = Math.floor(duration.asHours())
            const m = duration.minutes()
            const s = duration.seconds()
            return h > 0 
                ? `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`
                : `${m}:${s.toString().padStart(2, '0')}`
        }

        // WebSocket with auto-reconnect (VueUse)
        let ws = null
        
        const connectWebSocket = (taskId) => {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
            const wsUrl = `${protocol}//${window.location.host}/download/progress/ws/${taskId}`
            
            // Use reconnecting-websocket for reliability
            ws = new ReconnectingWebSocket(wsUrl, [], {
                maxRetries: 10,
                reconnectionDelayGrowFactor: 1.3,
                maxReconnectionDelay: 4000,
                minReconnectionDelay: 1000
            })
            
            ws.addEventListener('open', () => {
                console.log('WebSocket connected')
            })
            
            ws.addEventListener('message', (event) => {
                // Ignore pong messages
                if (event.data === 'pong') return
                
                try {
                    const data = JSON.parse(event.data)
                    downloadProgress.value = data
                    
                    if (data.status === 'completed') {
                        setTimeout(() => {
                            triggerBrowserDownload(taskId)
                        }, 500)
                    } else if (data.status === 'cancelled' || data.status === 'error') {
                        setTimeout(() => {
                            downloadProgress.value = null
                            downloading.value = false
                            currentTaskId.value = null
                            if (ws) {
                                ws.close()
                                ws = null
                            }
                        }, 2000)
                    }
                } catch (e) {
                    console.error('WebSocket message parse error:', e)
                }
            })
            
            ws.addEventListener('error', (error) => {
                console.error('WebSocket error:', error)
            })
            
            ws.addEventListener('close', () => {
                console.log('WebSocket disconnected')
            })

            // Send ping every 30 seconds
            const pingInterval = setInterval(() => {
                if (ws && ws.readyState === WebSocket.OPEN) {
                    ws.send('ping')
                } else {
                    clearInterval(pingInterval)
                }
            }, 30000)
        }

        // Download
        const triggerBrowserDownload = (taskId) => {
            const a = document.createElement('a')
            a.href = `/download/file/${taskId}`
            a.style.display = 'none'
            document.body.appendChild(a)
            a.click()
            
            setTimeout(() => {
                document.body.removeChild(a)
                downloadProgress.value = null
                downloading.value = false
                currentTaskId.value = null
                
                if (ws) {
                    ws.close()
                    ws = null
                }
                
                if (info.value) {
                    saveToHistory(info.value)
                }
            }, 1000)
        }

        const cancelDownload = async () => {
            if (!currentTaskId.value) return
            
            if (downloadProgress.value) {
                downloadProgress.value.status = 'cancelling'
                downloadProgress.value.message = 'キャンセル中...'
            }
            
            try {
                await api.post(`/download/cancel/${currentTaskId.value}`)
                console.log('Download cancelled')
            } catch (e) {
                console.error('Cancel error:', e)
                setTimeout(() => {
                    downloadProgress.value = null
                    downloading.value = false
                    currentTaskId.value = null
                    if (ws) {
                        ws.close()
                        ws = null
                    }
                }, 2000)
            }
        }

        const download = async () => {
            if (!info.value) return
            downloading.value = true
            error.value = null
            downloadProgress.value = {
                status: 'queued',
                progress: 0,
                message: 'ダウンロードを開始しています...'
            }
            
            try {
                const payload = { url: url.value }
                if (selectedQuality.value === 'audio') {
                    payload.audio_format = 'mp3'
                } else if (selectedQuality.value) {
                    payload.quality = parseInt(selectedQuality.value)
                }
                
                const response = await api.post('/download/start', payload)
                currentTaskId.value = response.data.task_id
                
                connectWebSocket(response.data.task_id)
            } catch (e) {
                error.value = e.message
                downloading.value = false
                downloadProgress.value = null
            }
        }

        // Ripple effect (keeping simple version, VueUse Motion is for complex animations)
        const addRipple = (event) => {
            const button = event.currentTarget
            const ripple = document.createElement('span')
            const rect = button.getBoundingClientRect()
            const size = Math.max(rect.width, rect.height)
            const x = event.clientX - rect.left - size / 2
            const y = event.clientY - rect.top - size / 2

            ripple.style.width = ripple.style.height = size + 'px'
            ripple.style.left = x + 'px'
            ripple.style.top = y + 'px'
            ripple.classList.add('ripple')

            button.appendChild(ripple)

            setTimeout(() => {
                ripple.remove()
            }, 600)
        }

        // Cleanup on unmount
        onMounted(() => {
            // VueUse handles everything automatically
        })

        // Return public API
        return {
            url, info: safeInfo, loading, downloading, error, selectedQuality,
            currentPage, showDeleteModal, showSettingsModal, history, isDarkMode, downloadProgress,
            availableQualities, currentTaskId,
            fetchInfo: fetchInfoImmediate, formatDuration, download, cancelDownload, navigateToPage,
            pasteFromClipboard, confirmClearHistory, clearHistory, deleteHistoryItem, loadFromHistory, 
            toggleTheme, toggleThemeManual, addRipple, formatTimestamp, formatRelativeTime
        }
    }
}).mount('#app')
