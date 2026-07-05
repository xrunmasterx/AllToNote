import { useState, useEffect } from 'react'
import { useModelStore } from '@/store/modelStore'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Button } from '@/components/ui/button'
import toast from 'react-hot-toast'

interface ModelSelectorProps {
  providerId: string
  manualOnly?: boolean
  defaultModel?: string
  onSaved?: () => void | Promise<void>
}

export function ModelSelector({
  providerId,
  manualOnly = false,
  defaultModel = '',
  onSaved,
}: ModelSelectorProps) {
  const { models, loading, selectedModel, loadModels, setSelectedModel, addNewModel } =
    useModelStore()
  const [search, setSearch] = useState('')
  const [manualModel, setManualModel] = useState(defaultModel)
  const [submitting, setSubmitting] = useState(false)

  const filteredModels = models.filter(model => {
    const keywords = search.trim().toLowerCase().split(/\s+/)
    const target = model.id.toLowerCase()
    return keywords.every(kw => target.includes(kw))
  })

  useEffect(() => {
    if (manualOnly) {
      setManualModel(defaultModel)
    } else if (providerId) {
      loadModels(providerId)
    }
  }, [defaultModel, manualOnly, providerId])

  const handleSubmit = async () => {
    const modelToSave = manualOnly ? manualModel.trim() : selectedModel
    if (!modelToSave) {
      toast.error('请选择一个模型')
      return
    }
    try {
      setSubmitting(true)
      await addNewModel(providerId, modelToSave)
      await onSaved?.()
      toast.success('保存模型成功 🎉')
    } catch (error) {
      toast.error('保存失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2 font-bold">
        <span>选择模型</span>
        {!manualOnly && (
          <Button
            variant="ghost"
            type="button"
            onClick={() => loadModels(providerId)}
            disabled={loading}
          >
            {loading ? '加载中...' : '刷新模型'}
          </Button>
        )}
      </div>

      {manualOnly ? (
        <Input
          value={manualModel}
          onChange={e => setManualModel(e.target.value)}
          placeholder="gpt-5.5"
          className="w-[300px]"
        />
      ) : (
        <Select value={selectedModel} onValueChange={setSelectedModel}>
          <SelectTrigger className="w-[300px]">
            <SelectValue placeholder="请选择模型" />
          </SelectTrigger>
          <SelectContent>
            <div className="p-2">
              <Input
                placeholder="搜索模型..."
                value={search}
                onChange={e => setSearch(e.target.value)}
                className="h-8"
              />
            </div>
            {filteredModels.map((model, index) => (
              <SelectItem key={`${model.id}-${index}`} value={model.id}>
                {model.id}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}

      <Button
        onClick={handleSubmit}
        disabled={submitting || !(manualOnly ? manualModel.trim() : selectedModel)}
      >
        {submitting ? '保存中...' : '保存模型'}
      </Button>
    </div>
  )
}
