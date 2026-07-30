import request from '@/utils/request.ts'

export const getDownloaderCookie = async (id: string) => {
  return await request.get('/get_downloader_cookie/' + id)
}

export const updateDownloaderCookie = async (data: { cookie: string; platform: string }) => {
  return await request.post('/update_downloader_cookie', data)
}

export const deleteDownloaderCookie = async (id: string) => {
  return await request.delete('/downloader_cookie/' + id)
}
