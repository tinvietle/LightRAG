import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { FileRejection, useDropzone } from 'react-dropzone'
import { ImageIcon, UploadIcon, XIcon, FileTextIcon } from 'lucide-react'
import { toast } from 'sonner'
import { useTranslation } from 'react-i18next'

import Button from '@/components/ui/Button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger
} from '@/components/ui/Dialog'
import { cn } from '@/lib/utils'
import { supportedFileTypes } from '@/lib/constants'
import { uploadCaseDocument } from '@/api/lightrag'

interface UploadDocumentsDialogProps {
  onDocumentsUploaded?: () => Promise<void>
}

type SelectedImage = {
  id: string
  file: File
  previewUrl: string
}

type SelectedTextCase = {
  id: string
  file: File
  images: SelectedImage[]
}

const createId = () => {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `case-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`
}

const revokeImages = (images: SelectedImage[]) => {
  images.forEach((image) => {
    URL.revokeObjectURL(image.previewUrl)
  })
}

const buildUploadError = (error: unknown, fallback: string) => {
  if (error && typeof error === 'object' && 'response' in error) {
    const axiosError = error as { response?: { status?: number; data?: { detail?: string } } }
    return axiosError.response?.data?.detail || fallback
  }
  return fallback
}

export default function UploadDocumentsDialog({ onDocumentsUploaded }: UploadDocumentsDialogProps) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [isUploading, setIsUploading] = useState(false)
  const [cases, setCases] = useState<SelectedTextCase[]>([])
  const [activeCaseId, setActiveCaseId] = useState<string | null>(null)
  const [serverImageLimit, setServerImageLimit] = useState<number | null>(null)
  const [caseProgresses, setCaseProgresses] = useState<Record<string, number>>({})
  const [caseErrors, setCaseErrors] = useState<Record<string, string>>({})
  const casesRef = useRef<SelectedTextCase[]>([])

  const activeCase = useMemo(
    () => cases.find((selectedCase) => selectedCase.id === activeCaseId) ?? null,
    [cases, activeCaseId]
  )

  useEffect(() => {
    casesRef.current = cases
  }, [cases])

  const resetDialogState = useCallback(() => {
    setCases((currentCases) => {
      currentCases.forEach((selectedCase) => revokeImages(selectedCase.images))
      return []
    })
    setActiveCaseId(null)
    setCaseProgresses({})
    setCaseErrors({})
  }, [])

  useEffect(() => {
    return () => {
      casesRef.current.forEach((selectedCase) => revokeImages(selectedCase.images))
    }
  }, [])

  useEffect(() => {
    // Fetch runtime config from server to determine image upload limit
    fetch('/api/config')
      .then((res) => res.json())
      .then((data) => {
        if (data && data.image_upload_limit) {
          setServerImageLimit(Number(data.image_upload_limit))
        }
      })
      .catch(() => {
        /* ignore */
      })
  }, [])

  const addTextCases = useCallback(
    (acceptedFiles: File[], rejectedFiles: FileRejection[]) => {
      if (rejectedFiles.length > 0) {
        rejectedFiles.forEach(({ file }) => {
          toast.error(
            t('documentPanel.uploadDocuments.fileUploader.fileRejected', { name: file.name })
          )
        })
      }

      if (!acceptedFiles.length) {
        return
      }

      const nextCases = acceptedFiles.map((file) => ({
        id: createId(),
        file,
        images: [] as SelectedImage[]
      }))

      setCases((currentCases) => [...currentCases, ...nextCases])
      setActiveCaseId((currentActive) => currentActive ?? nextCases[0].id)
    },
    [t]
  )

  const addImagesToActiveCase = useCallback(
    (acceptedFiles: File[], rejectedFiles: FileRejection[]) => {
      if (!activeCaseId) {
        toast.error(
          t('documentPanel.uploadDocuments.staged.selectTextFirst', 'Select a text file first.')
        )
        return
      }

      if (rejectedFiles.length > 0) {
        rejectedFiles.forEach(({ file }) => {
          toast.error(
            t('documentPanel.uploadDocuments.fileUploader.fileRejected', { name: file.name })
          )
        })
      }

      if (!acceptedFiles.length) {
        return
      }

      setCases((currentCases) => {
        const nextCases = currentCases.map((selectedCase) => {
          if (selectedCase.id !== activeCaseId) {
            return selectedCase
          }

          const limit = serverImageLimit ?? 10
          const availableSlots = Math.max(0, limit - selectedCase.images.length)
          if (availableSlots <= 0) {
            toast.error(
              t('documentPanel.uploadDocuments.staged.imageLimit', `You can attach up to ${limit} images per text file.`)
            )
            return selectedCase
          }

          const filesToAdd = acceptedFiles.slice(0, availableSlots)
          if (acceptedFiles.length > availableSlots) {
            toast.error(
              t(
                'documentPanel.uploadDocuments.staged.imageLimitPartial',
                'Only the first {{count}} images were added.',
                { count: availableSlots }
              )
            )
          }

          const nextImages = filesToAdd.map((file) => ({
            id: createId(),
            file,
            previewUrl: URL.createObjectURL(file)
          }))

          return {
            ...selectedCase,
            images: [...selectedCase.images, ...nextImages]
          }
        })

        return nextCases
      })
    },
    [activeCaseId, t]
  )

  const removeCase = useCallback((caseId: string) => {
    setCases((currentCases) => {
      const selectedCase = currentCases.find((entry) => entry.id === caseId)
      if (selectedCase) {
        revokeImages(selectedCase.images)
      }

      const nextCases = currentCases.filter((entry) => entry.id !== caseId)
      return nextCases
    })

    setCaseProgresses((current) => {
      const next = { ...current }
      delete next[caseId]
      return next
    })

    setCaseErrors((current) => {
      const next = { ...current }
      delete next[caseId]
      return next
    })

    setActiveCaseId((currentActive) => {
      if (currentActive !== caseId) {
        return currentActive
      }
      return null
    })
  }, [])

  const removeImage = useCallback((caseId: string, imageId: string) => {
    setCases((currentCases) =>
      currentCases.map((selectedCase) => {
        if (selectedCase.id !== caseId) {
          return selectedCase
        }

        const image = selectedCase.images.find((entry) => entry.id === imageId)
        if (image) {
          URL.revokeObjectURL(image.previewUrl)
        }

        return {
          ...selectedCase,
          images: selectedCase.images.filter((entry) => entry.id !== imageId)
        }
      })
    )
  }, [])

  const handleUploadConfirmed = useCallback(async () => {
    if (!cases.length) {
      toast.error(
        t(
          'documentPanel.uploadDocuments.staged.requireText',
          'Add at least one text file before uploading.'
        )
      )
      return
    }

    setIsUploading(true)
    const toastId = toast.loading(t('documentPanel.uploadDocuments.batch.uploading'))
    let successCount = 0
    let failureCount = 0
    const successfulCaseIds: string[] = []
    const uploadOrder = [...cases].sort((left, right) => left.file.name.localeCompare(right.file.name))

    try {
      for (const selectedCase of uploadOrder) {
        try {
          setCaseProgresses((current) => ({
            ...current,
            [selectedCase.id]: 0
          }))

          const result = await uploadCaseDocument(
            selectedCase.file,
            selectedCase.images.map((image) => image.file),
            (percentCompleted: number) => {
              setCaseProgresses((current) => ({
                ...current,
                [selectedCase.id]: percentCompleted
              }))
            }
          )

          if (result.status === 'duplicated') {
            const message = t('documentPanel.uploadDocuments.fileUploader.duplicateFile')
            setCaseErrors((current) => ({
              ...current,
              [selectedCase.id]: message
            }))
            toast.error(message)
            failureCount += 1
            continue
          }

          if (result.status !== 'success') {
            const message = result.message
            setCaseErrors((current) => ({
              ...current,
              [selectedCase.id]: message
            }))
            toast.error(message)
            failureCount += 1
            continue
          }

          successCount += 1
          successfulCaseIds.push(selectedCase.id)
        } catch (error) {
          const message = buildUploadError(
            error,
            t('documentPanel.uploadDocuments.generalError', {
              error: t('documentPanel.uploadDocuments.batch.error')
            })
          )
          setCaseErrors((current) => ({
            ...current,
            [selectedCase.id]: message
          }))
          setCaseProgresses((current) => ({
            ...current,
            [selectedCase.id]: 100
          }))
          toast.error(message)
          failureCount += 1
        }
      }

      if (successfulCaseIds.length > 0) {
        setCases((currentCases) => currentCases.filter((selectedCase) => !successfulCaseIds.includes(selectedCase.id)))
        setCaseProgresses((currentProgresses) => {
          const nextProgresses = { ...currentProgresses }
          successfulCaseIds.forEach((caseId) => {
            delete nextProgresses[caseId]
          })
          return nextProgresses
        })
        setCaseErrors((currentErrors) => {
          const nextErrors = { ...currentErrors }
          successfulCaseIds.forEach((caseId) => {
            delete nextErrors[caseId]
          })
          return nextErrors
        })
        setActiveCaseId((currentActive) =>
          currentActive && successfulCaseIds.includes(currentActive) ? null : currentActive
        )
      }

      if (successCount > 0 && failureCount === 0) {
        toast.success(t('documentPanel.uploadDocuments.batch.success'), { id: toastId })
        if (onDocumentsUploaded) {
          await onDocumentsUploaded()
        }
        resetDialogState()
        setOpen(false)
      } else if (successCount > 0) {
        toast.error(t('documentPanel.uploadDocuments.batch.error'), { id: toastId })
        if (onDocumentsUploaded) {
          await onDocumentsUploaded()
        }
      } else {
        toast.error(t('documentPanel.uploadDocuments.batch.error'), { id: toastId })
      }
    } catch (error) {
      toast.error(
        t('documentPanel.uploadDocuments.generalError', {
          error: buildUploadError(error, t('documentPanel.uploadDocuments.batch.error'))
        }),
        { id: toastId }
      )
    } finally {
      setIsUploading(false)
    }
  }, [cases, onDocumentsUploaded, resetDialogState, t])

  const textDropzone = useDropzone({
    accept: supportedFileTypes,
    multiple: true,
    disabled: isUploading,
    onDrop: addTextCases
  })

  const imageDropzone = useDropzone({
    accept: { 'image/*': [] },
    multiple: true,
    disabled: isUploading || !activeCase,
    onDrop: addImagesToActiveCase
  })

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (isUploading) {
          return
        }

        if (!nextOpen) {
          resetDialogState()
        }

        setOpen(nextOpen)
      }}
    >
      <DialogTrigger asChild>
        <Button variant="default" side="bottom" tooltip={t('documentPanel.uploadDocuments.tooltip')} size="sm">
          <UploadIcon /> {t('documentPanel.uploadDocuments.button')}
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-5xl" onCloseAutoFocus={(event) => event.preventDefault()}>
        <DialogHeader>
          <DialogTitle>{t('documentPanel.uploadDocuments.title')}</DialogTitle>
          <DialogDescription>{t('documentPanel.uploadDocuments.description')}</DialogDescription>
        </DialogHeader>

        <div className="grid gap-4 lg:grid-cols-2">
          <section className="flex flex-col gap-3 rounded-xl border border-border/60 bg-muted/20 p-4">
            <div className="flex items-center justify-between gap-2">
              <div>
                <h3 className="text-sm font-semibold">
                  {t('documentPanel.uploadDocuments.staged.textTitle', 'Text files')}
                </h3>
                <p className="text-muted-foreground text-xs">
                  {t(
                    'documentPanel.uploadDocuments.staged.textDescription',
                    'Drop one or more text files. Select a file to attach images.'
                  )}
                </p>
              </div>
              <Button type="button" variant="outline" size="sm" onClick={textDropzone.open} disabled={isUploading}>
                {t('documentPanel.uploadDocuments.staged.addText', 'Add text')}
              </Button>
            </div>

            <div
              {...textDropzone.getRootProps()}
              className={cn(
                'border-muted-foreground/25 hover:bg-muted/60 flex min-h-40 cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed px-4 py-6 text-center transition',
                textDropzone.isDragActive && 'border-primary bg-primary/5'
              )}
            >
              <input {...textDropzone.getInputProps()} />
              <div className="flex flex-col items-center gap-3">
                <div className="bg-background flex size-11 items-center justify-center rounded-full border shadow-sm">
                  <FileTextIcon className="text-muted-foreground size-5" aria-hidden="true" />
                </div>
                <div className="space-y-1">
                  <p className="text-sm font-medium">
                    {textDropzone.isDragActive
                      ? t('documentPanel.uploadDocuments.fileUploader.dropHere')
                      : t('documentPanel.uploadDocuments.fileUploader.dragAndDrop')}
                  </p>
                  <p className="text-muted-foreground text-xs">
                    {t(
                      'documentPanel.uploadDocuments.staged.textHint',
                      'Click to choose files or drag them here. Text is required.'
                    )}
                  </p>
                </div>
              </div>
            </div>

            <div className="flex max-h-72 flex-col gap-2 overflow-auto pr-1">
              {cases.length === 0 ? (
                <div className="text-muted-foreground rounded-lg border border-dashed px-4 py-6 text-center text-sm">
                  {t('documentPanel.uploadDocuments.staged.emptyText', 'No text files selected yet.')}
                </div>
              ) : (
                cases.map((selectedCase) => {
                  const isActive = selectedCase.id === activeCaseId
                  const progress = caseProgresses[selectedCase.id] ?? 0
                  const error = caseErrors[selectedCase.id]

                  return (
                    <button
                      key={selectedCase.id}
                      type="button"
                      className={cn(
                        'flex w-full flex-col gap-2 rounded-lg border px-3 py-3 text-left transition',
                        isActive ? 'border-primary bg-primary/5' : 'border-border/60 bg-background hover:bg-muted/40'
                      )}
                      onClick={() => setActiveCaseId(selectedCase.id)}
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-sm font-medium">{selectedCase.file.name}</p>
                          <p className="text-muted-foreground text-xs">
                            {selectedCase.images.length}{' '}
                            {t('documentPanel.uploadDocuments.staged.imageCount', 'image(s) attached')}
                          </p>
                        </div>
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon"
                          className="size-7 shrink-0"
                          onClick={(event) => {
                            event.stopPropagation()
                            removeCase(selectedCase.id)
                          }}
                          disabled={isUploading}
                        >
                          <XIcon className="size-4" aria-hidden="true" />
                          <span className="sr-only">
                            {t('documentPanel.uploadDocuments.fileUploader.removeFile')}
                          </span>
                        </Button>
                      </div>

                      <div className="h-1.5 overflow-hidden rounded-full bg-secondary">
                        <div
                          className={cn('h-full rounded-full transition-all', error ? 'bg-red-400' : 'bg-primary')}
                          style={{ width: `${progress}%` }}
                        />
                      </div>

                      {error ? <p className="text-red-500 text-xs">{error}</p> : null}
                    </button>
                  )
                })
              )}
            </div>
          </section>

          <section className="flex flex-col gap-3 rounded-xl border border-border/60 bg-muted/20 p-4">
            <div className="flex items-center justify-between gap-2">
              <div>
                <h3 className="text-sm font-semibold">
                  {t('documentPanel.uploadDocuments.staged.imageTitle', 'Images')}
                </h3>
                <p className="text-muted-foreground text-xs">
                  {t(
                    'documentPanel.uploadDocuments.staged.imageDescription',
                    'Attach up to 10 images to the selected text file.'
                  )}
                </p>
              </div>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={imageDropzone.open}
                disabled={isUploading || !activeCase}
              >
                {t('documentPanel.uploadDocuments.staged.addImages', 'Add images')}
              </Button>
            </div>

            <div
              {...imageDropzone.getRootProps()}
              className={cn(
                'border-muted-foreground/25 hover:bg-muted/60 flex min-h-40 cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed px-4 py-6 text-center transition',
                imageDropzone.isDragActive && 'border-primary bg-primary/5',
                !activeCase && 'opacity-60'
              )}
            >
              <input {...imageDropzone.getInputProps()} />
              <div className="flex flex-col items-center gap-3">
                <div className="bg-background flex size-11 items-center justify-center rounded-full border shadow-sm">
                  <ImageIcon className="text-muted-foreground size-5" aria-hidden="true" />
                </div>
                <div className="space-y-1">
                  <p className="text-sm font-medium">
                    {activeCase
                      ? imageDropzone.isDragActive
                        ? t('documentPanel.uploadDocuments.fileUploader.dropHere')
                        : t('documentPanel.uploadDocuments.fileUploader.dragAndDrop')
                      : t('documentPanel.uploadDocuments.staged.selectTextFirst', 'Select a text file first.')}
                  </p>
                  <p className="text-muted-foreground text-xs">
                    {t(
                      'documentPanel.uploadDocuments.staged.imageDescription',
                      `Attach up to ${serverImageLimit ?? 10} images to the selected text file.`
                    )}
                  </p>
                  <p className="text-muted-foreground text-xs">
                    {activeCase
                      ? t(
                          'documentPanel.uploadDocuments.staged.imageHint',
                          'Click to choose images or drag them here.'
                        )
                      : t(
                          'documentPanel.uploadDocuments.staged.imageHintInactive',
                          'Images are linked to the selected text case.'
                        )}
                  </p>
                </div>
              </div>
            </div>

            {activeCase ? (
              <div className="space-y-2">
                <div className="flex items-center justify-between gap-2">
                  <p className="text-sm font-medium">{activeCase.file.name}</p>
                  <p className="text-muted-foreground text-xs">{activeCase.images.length}/{serverImageLimit ?? 10}</p>
                </div>

                {activeCase.images.length ? (
                  <div className="grid max-h-72 grid-cols-2 gap-2 overflow-auto pr-1 sm:grid-cols-3">
                    {activeCase.images.map((image) => (
                      <div key={image.id} className="group relative overflow-hidden rounded-lg border bg-background">
                        <img
                          src={image.previewUrl}
                          alt={image.file.name}
                          className="h-24 w-full object-cover"
                        />
                        <div className="flex items-center justify-between gap-2 px-2 py-1">
                          <p className="truncate text-[11px] text-foreground/80">{image.file.name}</p>
                          <button
                            type="button"
                            className="text-muted-foreground hover:text-foreground"
                            onClick={() => removeImage(activeCase.id, image.id)}
                            disabled={isUploading}
                          >
                            <XIcon className="size-3.5" aria-hidden="true" />
                            <span className="sr-only">
                              {t('documentPanel.uploadDocuments.fileUploader.removeFile')}
                            </span>
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="text-muted-foreground rounded-lg border border-dashed px-4 py-6 text-center text-sm">
                    {t('documentPanel.uploadDocuments.staged.emptyImages', 'No images attached yet.')}
                  </div>
                )}
              </div>
            ) : null}
          </section>
        </div>

        <div className="flex items-center justify-between gap-3 pt-1">
          <div className="text-muted-foreground text-xs">
            {t(
              'documentPanel.uploadDocuments.staged.footerNote',
              'Text is required. Images are optional and stay linked to the selected text case.'
            )}
          </div>
          <div className="flex items-center gap-2">
            <Button type="button" variant="outline" onClick={() => setOpen(false)} disabled={isUploading}>
              {t('common.cancel', 'Cancel')}
            </Button>
            <Button
              type="button"
              variant="default"
              onClick={handleUploadConfirmed}
              disabled={isUploading || cases.length === 0}
            >
              {isUploading
                ? t('documentPanel.uploadDocuments.batch.uploading')
                : t('documentPanel.uploadDocuments.staged.confirmButton', 'Confirm upload')}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
