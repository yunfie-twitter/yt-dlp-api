/**
 * yt-dlp Web Client
 * Main application logic
 */

const { createApp, ref, computed, onMounted, onUnmounted } = Vue

createApp({
    setup() {
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
        const history = ref([])
        const isDarkMode = ref(true)
        const downloadProgress = ref(null)
        const websocket = ref(null)
        const currentTaskId = ref(null)

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

        // UI Effects
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

        // Cookie management
        const setCookie = (name, value, days = 365) => {
            const expires = new Date()
            expires.setTime(expires.getTime() + days * 24 * 60 * 60 * 1000)
            document.cookie = `${name}=${encodeURIComponent(JSON.stringify(value))};expires=${expires.toUTCString()};path=/`
        }

        const getCookie = (name) => {
            const nameEQ = name + '='
            const ca = document.cookie.split(';')
            for (let i = 0; i < ca.length; i++) {
                let c = ca[i]
                while (c.charAt(0) === ' ') c = c.substring(1, c.length)
                if (c.indexOf(nameEQ) === 0) {
                    return JSON.parse(decodeURIComponent(c.substring(nameEQ.length, c.length)))
                }
            }
            return null
        }

        // History management
        const loadHistory = () => {
            const saved = getCookie('downloadHistory')
            if (saved) {
                history.value = saved
            }
        }

        const saveToHistory = (videoInfo) => {
            const historyItem = {
                url: url.value,
                title: videoInfo.title,
                thumbnail: videoInfo.thumbnail,
                uploader: videoInfo.uploader,
                timestamp: Date.now()
            }
            history.value.unshift(historyItem)
            if (history.value.length > 20) {
                history.value = history.value.slice(0, 20)
            }
            setCookie('downloadHistory', history.value)
        }

        const confirmClearHistory = () => {
            showDeleteModal.value = true
        }

        const clearHistory = () => {
            history.value = []
            setCookie('downloadHistory', [])
            showDeleteModal.value = false
        }

        const deleteHistoryItem = (index) => {
            history.value.splice(index, 1)
            setCookie('downloadHistory', history.value)
        }

        const loadFromHistory = (item) => {
            url.value = item.url
            currentPage.value = 'home'
            fetchInfo()
        }

        // Navigation
        const navigateToPage = (page) => {
            currentPage.value = page
        }

        // Theme management
        const toggleTheme = (event) => {
            isDarkMode.value = event.target.checked
            if (isDarkMode.value) {
                document.body.classList.add('dark')
                document.body.classList.remove('light')
            } else {
                document.body.classList.add('light')
                document.body.classList.remove('dark')
            }
        }

        const toggleThemeManual = () => {
            isDarkMode.value = !isDarkMode.value
            if (isDarkMode.value) {
                document.body.classList.add('dark')
                document.body.classList.remove('light')
            } else {
                document.body.classList.add('light')
                document.body.classList.remove('dark')
            }
        }

        // Clipboard
        const pasteFromClipboard = async () => {
            try {
                const text = await navigator.clipboard.readText()
                url.value = text
            } catch (e) {
                error.value = 'クリップボードからの読み取りに失敗しました'
            }
        }

        // Video info
        const fetchInfo = async () => {
            if (!url.value) return
            loading.value = true
            error.value = null
            info.value = null
            
            try {
                const res = await fetch('/info', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url: url.value })
                })
                if (!res.ok) {
                    const errorData = await res.json()
                    throw new Error(errorData.detail || '情報の取得に失敗しました')
                }
                info.value = await res.json()
            } catch (e) {
                error.value = e.message
            } finally {
                loading.value = false
            }
        }

        const formatDuration = (seconds) => {
            if (!seconds) return 'Live'
            const h = Math.floor(seconds / 3600)
            const m = Math.floor((seconds % 3600) / 60)
            const s = seconds % 60
            return h > 0 
                ? `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`
                : `${m}:${s.toString().padStart(2, '0')}`
        }

        // WebSocket connection
        const connectWebSocket = (taskId) => {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
            const wsUrl = `${protocol}//${window.location.host}/download/progress/ws/${taskId}`
            
            websocket.value = new WebSocket(wsUrl)
            
            websocket.value.onopen = () => {
                console.log('WebSocket connected')
                const pingInterval = setInterval(() => {
                    if (websocket.value?.readyState === WebSocket.OPEN) {
                        websocket.value.send('ping')
                    } else {
                        clearInterval(pingInterval)
                    }
                }, 30000)
            }
            
            websocket.value.onmessage = (event) => {
                // Ignore pong messages from server
                if (event.data === 'pong') {
                    return
                }
                
                try {
                    const data = JSON.parse(event.data)
                    downloadProgress.value = data
                    
                    if (data.status === 'completed') {
                        setTimeout(() => {
                            triggerBrowserDownload(taskId)
                        }, 500)
                    } else if (data.status === 'cancelled' || data.status === 'error') {
                        // Wait a bit to show the status, then cleanup
                        setTimeout(() => {
                            downloadProgress.value = null
                            downloading.value = false
                            currentTaskId.value = null
                            if (websocket.value) {
                                websocket.value.close()
                                websocket.value = null
                            }
                        }, 2000)
                    }
                } catch (e) {
                    console.error('WebSocket message parse error:', e)
                }
            }
            
            websocket.value.onerror = (error) => {
                console.error('WebSocket error:', error)
            }
            
            websocket.value.onclose = () => {
                console.log('WebSocket disconnected')
            }
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
                
                // Close WebSocket after file download
                if (websocket.value) {
                    websocket.value.close()
                    websocket.value = null
                }
                
                if (info.value) {
                    saveToHistory(info.value)
                }
            }, 1000)
        }

        const cancelDownload = async () => {
            if (!currentTaskId.value) return
            
            // Show cancelling status immediately
            if (downloadProgress.value) {
                downloadProgress.value.status = 'cancelling'
                downloadProgress.value.message = 'キャンセル中...'
            }
            
            try {
                const res = await fetch(`/download/cancel/${currentTaskId.value}`, {
                    method: 'POST'
                })
                
                if (res.ok) {
                    console.log('Download cancelled')
                } else {
                    console.error('Cancel failed')
                    // Still cleanup on error
                    setTimeout(() => {
                        downloadProgress.value = null
                        downloading.value = false
                        currentTaskId.value = null
                        if (websocket.value) {
                            websocket.value.close()
                            websocket.value = null
                        }
                    }, 2000)
                }
            } catch (e) {
                console.error('Cancel error:', e)
                // Cleanup on network error
                setTimeout(() => {
                    downloadProgress.value = null
                    downloading.value = false
                    currentTaskId.value = null
                    if (websocket.value) {
                        websocket.value.close()
                        websocket.value = null
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
                    // For audio-only, set audio_format flag
                    payload.audio_format = 'mp3'
                } else if (selectedQuality.value) {
                    payload.quality = parseInt(selectedQuality.value)
                }
                
                const res = await fetch('/download/start', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                })
                
                if (!res.ok) throw new Error('ダウンロードの開始に失敗しました')
                
                const data = await res.json()
                currentTaskId.value = data.task_id
                
                connectWebSocket(data.task_id)
                
            } catch (e) {
                error.value = e.message
                downloading.value = false
                downloadProgress.value = null
            }
        }

        // Lifecycle hooks
        onUnmounted(() => {
            if (websocket.value) {
                websocket.value.close()
            }
        })

        onMounted(() => {
            loadHistory()
        })

        // Return public API
        return {
            url, info, loading, downloading, error, selectedQuality,
            currentPage, showDeleteModal, showSettingsModal, history, isDarkMode, downloadProgress,
            availableQualities, currentTaskId,
            fetchInfo, formatDuration, download, cancelDownload, navigateToPage,
            pasteFromClipboard, confirmClearHistory, clearHistory, deleteHistoryItem, loadFromHistory, 
            toggleTheme, toggleThemeManual, addRipple
        }
    }
}).mount('#app')
