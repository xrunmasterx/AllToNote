// 下载器 Cookie 设置表单（最简化版）
import { useForm } from 'react-hook-form'
import { z } from 'zod'
import { zodResolver } from '@hookform/resolvers/zod'
import {
  Form,
  FormField,
  FormItem,
  FormLabel,
  FormControl,
  FormMessage,
} from '@/components/ui/form'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { useEffect, useState } from 'react'
import toast from 'react-hot-toast'
import {
  deleteDownloaderCookie,
  getDownloaderCookie,
  updateDownloaderCookie,
} from '@/services/downloader'
import { useParams } from 'react-router-dom'
import { videoPlatforms } from '@/constant/note.ts'

const CookieSchema = z.object({
  cookie: z.string().min(10, '请填写有效 Cookie'),
})

const DownloaderForm = () => {
  const form = useForm({
    resolver: zodResolver(CookieSchema),
    defaultValues: { cookie: '' },
  })
  const { id } = useParams()

  const [loading, setLoading] = useState(true)
  const [configured, setConfigured] = useState(false)

  useEffect(() => {
    const loadCookie = async () => {
      setLoading(true) // 🔁 切换平台时显示 loading
      try {
        const res = await getDownloaderCookie(id)
        setConfigured(Boolean(res?.configured))
        form.reset({ cookie: '' })
      } catch (e) {
        toast.error('加载 Cookie 失败: ' + e)
        setConfigured(false)
        form.reset({ cookie: '' })
      } finally {
        setLoading(false)
      }
    }

    if (id) loadCookie()
  }, [form, id]) // 🔁 每当 id 变化时触发

  const onSubmit = async values => {
    if (!id) return
    try {
      await updateDownloaderCookie({
        platform: id,
        cookie: String(values.cookie),
      })
      setConfigured(true)
      form.reset({ cookie: '' })
      toast.success('保存成功')
    } catch {
      toast.error('保存失败')
    }
  }

  const onDelete = async () => {
    if (!id) return
    try {
      await deleteDownloaderCookie(id)
      setConfigured(false)
      form.reset({ cookie: '' })
      toast.success('已删除保存的 Cookie')
    } catch {
      toast.error('删除失败')
    }
  }

  if (loading) return <div className="p-4">加载中...</div>

  return (
    <div className="max-w-xl p-4">
      <Form {...form}>
        <form onSubmit={form.handleSubmit(onSubmit)} className="flex flex-col gap-4">
          <div className="text-lg font-bold">
            设置{videoPlatforms.find(item => item.value === id)?.label}下载器 Cookie
          </div>

          <div className="text-sm text-muted-foreground">
            {configured
              ? '已安全保存。为保护账号信息，页面不会回显 Cookie；输入新值可覆盖。'
              : '尚未配置 Cookie。保存后将写入系统凭据库。'}
          </div>

          <FormField
            control={form.control}
            name="cookie"
            render={({ field }) => (
              <FormItem className="flex flex-col gap-2">
                <FormLabel>Cookie</FormLabel>
                <FormControl>
                  <Input
                    {...field}
                    type="password"
                    autoComplete="new-password"
                    placeholder={configured ? '输入新 Cookie 以覆盖' : '输入 Cookie'}
                  />
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />

          <div className="flex gap-2">
            <Button type="submit">保存</Button>
            {configured && (
              <Button type="button" variant="outline" onClick={onDelete}>
                删除已保存 Cookie
              </Button>
            )}
          </div>
        </form>
      </Form>
    </div>
  )
}

export default DownloaderForm
