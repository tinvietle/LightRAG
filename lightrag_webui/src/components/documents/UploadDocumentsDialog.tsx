import { useCallback, useMemo, useState } from 'react'
import Dropzone, { FileRejection } from 'react-dropzone'
import { toast } from 'sonner'
import { useTranslation } from 'react-i18next'
import { FileText, ImageIcon, UploadIcon, XIcon } from 'lucide-react'

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger
} from '@/components/ui/Dialog'
import Button from '@/components/ui/Button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/Card'
import { ScrollArea } from '@/components/ui/ScrollArea'
import Progress from '@/components/ui/Progress'
import { cn, errorMessage } from '@/lib/utils'
import {
  multimodalImageFileTypes,
  multimodalTextCaseFileTypes
} from '@/lib/constants'
import { uploadMultimodalCaseDocument } from '@/api/lightrag'
import { useBackendState } from '@/stores/state'

interface UploadDocumentsDialogProps {
  onDocumentsUploaded?: () => Promise<void>
  onUploadBatchAccepted?: () => void
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

const createId = () =>
  globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`

const revokeImagePreview = (image: SelectedImage) => {
  URL.revokeObjectURL(image.previewUrl)
}

const revokeCasePreviews = (selectedCase: SelectedTextCase) => {
  selectedCase.images.forEach(revokeImagePreview)
}

const formatDropzoneErrors = (
  rejectedFiles: FileRejection[],
  unsupportedTypeMessage: string,
  genericRejectedMessage: (name: string) => string
): string[] =>
  rejectedFiles.map(({ file, errors }) => {
    const firstError = errors[0]?.message || genericRejectedMessage(file.name)
    if (firstError.includes('file-invalid-type')) {
      return unsupportedTypeMessage
    }
    return firstError
  })

export default function UploadDocumentsDialog({
  onDocumentsUploaded,
  onUploadBatchAccepted
}: UploadDocumentsDialogProps) {
  const { t } = useTranslation()
  const healthStatus = useBackendState.use.status()
  const [open, setOpen] = useState(false)
  const [isUploading, setIsUploading] = useState(false)
  const [cases, setCases] = useState<SelectedTextCase[]>([])
  const [activeCaseId, setActiveCaseId] = useState<string | null>(null)
  const [progresses, setProgresses] = useState<Record<string, number>>({})
  const [caseErrors, setCaseErrors] = useState<Record<string, string>>({})

  const maxCaseImages = healthStatus?.configuration?.max_multimodal_case_images ?? 5
  const activeCase = useMemo(
    () => cases.find((selectedCase) => selectedCase.id === activeCaseId) ?? null,
    [activeCaseId, cases]
  )

  const resetDialogState = useCallback(() => {
    setCases((currentCases) => {
      currentCases.forEach(revokeCasePreviews)
      return []
    })
    setActiveCaseId(null)
    setProgresses({})
    setCaseErrors({})
    setIsUploading(false)
  }, [])

  const handleOpenChange = useCallback(
    (nextOpen: boolean) => {
      if (isUploading) {
        return
      }
      setOpen(nextOpen)
      if (!nextOpen) {
        resetDialogState()
      }
    },
    [isUploading, resetDialogState]
  )

  const handleRejectedFiles = useCallback(
    (rejectedFiles: FileRejection[]) => {
      const unsupportedTypeMessage = t(
        'documentPanel.uploadDocuments.fileUploader.unsupportedType'
      )
      const genericRejectedMessage = (name: string) =>
        t('documentPanel.uploadDocuments.fileUploader.fileRejected', { name })

      formatDropzoneErrors(
        rejectedFiles,
        unsupportedTypeMessage,
        genericRejectedMessage
      ).forEach((message) => {
        toast.error(message)
      })
    },
    [t]
  )

  const addTextCases = useCallback(
    (acceptedFiles: File[], rejectedFiles: FileRejection[]) => {
      handleRejectedFiles(rejectedFiles)
      if (!acceptedFiles.length) {
        return
      }

      setCases((currentCases) => {
        const existingNames = new Set(currentCases.map((selectedCase) => selectedCase.file.name))
        const nextCases = [...currentCases]
        let nextActiveCaseId = activeCaseId

        acceptedFiles.forEach((file) => {
          if (existingNames.has(file.name)) {
            toast.error(
              t('documentPanel.uploadDocuments.caseAlreadyStaged', {
                defaultValue: `Case ${file.name} is already staged.`,
                name: file.name
              })
            )
            return
          }

          const nextCase: SelectedTextCase = {
            id: createId(),
            file,
            images: []
          }
          nextCases.push(nextCase)
          existingNames.add(file.name)
          nextActiveCaseId = nextCase.id
        })

        setActiveCaseId(nextActiveCaseId ?? nextCases[0]?.id ?? null)
        return nextCases
      })
    },
    [activeCaseId, handleRejectedFiles, t]
  )

  const addImagesToActiveCase = useCallback(
    (acceptedFiles: File[], rejectedFiles: FileRejection[]) => {
      handleRejectedFiles(rejectedFiles)

      if (!activeCaseId) {
        toast.error(
          t('documentPanel.uploadDocuments.selectCaseFirst', {
            defaultValue: 'Select a text case before attaching images.'
          })
        )
        return
      }

      if (!acceptedFiles.length) {
        return
      }

      setCases((currentCases) => {
        return currentCases.map((selectedCase) => {
          if (selectedCase.id !== activeCaseId) {
            return selectedCase
          }

          if (selectedCase.images.length + acceptedFiles.length > maxCaseImages) {
            toast.error(
              t('documentPanel.uploadDocuments.imageLimitReached', {
                defaultValue: `You can attach up to ${maxCaseImages} images to a single case.`,
                count: maxCaseImages
              })
            )
            return selectedCase
          }

          const existingImageNames = new Set(selectedCase.images.map((image) => image.file.name))
          const nextImages = acceptedFiles.flatMap((file) => {
            if (existingImageNames.has(file.name)) {
              toast.error(
                t('documentPanel.uploadDocuments.imageAlreadyAttached', {
                  defaultValue: `Image ${file.name} is already attached to this case.`,
                  name: file.name
                })
              )
              return []
            }

            existingImageNames.add(file.name)
            return [
              {
                id: createId(),
                file,
                previewUrl: URL.createObjectURL(file)
              }
            ]
          })

          return {
            ...selectedCase,
            images: [...selectedCase.images, ...nextImages]
          }
        })
      })
    },
    [activeCaseId, handleRejectedFiles, maxCaseImages, t]
  )

  const removeCase = useCallback((caseId: string) => {
    setCases((currentCases) => {
      const selectedCase = currentCases.find((candidate) => candidate.id === caseId)
      if (selectedCase) {
        revokeCasePreviews(selectedCase)
      }

      const nextCases = currentCases.filter((candidate) => candidate.id !== caseId)
      setActiveCaseId((currentActiveCaseId) => {
        if (currentActiveCaseId !== caseId) {
          return currentActiveCaseId
        }
        return nextCases[0]?.id ?? null
      })
      setProgresses((currentProgresses) => {
        const nextProgresses = { ...currentProgresses }
        delete nextProgresses[caseId]
        return nextProgresses
      })
      setCaseErrors((currentErrors) => {
        const nextErrors = { ...currentErrors }
        delete nextErrors[caseId]
        return nextErrors
      })
      return nextCases
    })
  }, [])

  const removeImageFromCase = useCallback((caseId: string, imageId: string) => {
    setCases((currentCases) =>
      currentCases.map((selectedCase) => {
        if (selectedCase.id !== caseId) {
          return selectedCase
        }

        const imageToRemove = selectedCase.images.find((image) => image.id === imageId)
        if (imageToRemove) {
          revokeImagePreview(imageToRemove)
        }

        return {
          ...selectedCase,
          images: selectedCase.images.filter((image) => image.id !== imageId)
        }
      })
    )
  }, [])

  const handleDocumentsUpload = useCallback(async () => {
    if (!cases.length) {
      toast.error(
        t('documentPanel.uploadDocuments.noCases', {
          defaultValue: 'Add at least one text case before uploading.'
        })
      )
      return
    }

    setIsUploading(true)
    const toastId = toast.loading(
      t('documentPanel.uploadDocuments.batch.uploading')
    )
    const sortedCases = [...cases].sort((left, right) =>
      left.file.name.localeCompare(right.file.name, undefined, {
        numeric: true,
        sensitivity: 'base'
      })
    )
    const successfulCaseIds = new Set<string>()
    let hasSuccessfulUpload = false
    let batchProbeTriggered = false

    try {
      for (const selectedCase of sortedCases) {
        setProgresses((currentProgresses) => ({
          ...currentProgresses,
          [selectedCase.id]: 0
        }))
        setCaseErrors((currentErrors) => {
          const nextErrors = { ...currentErrors }
          delete nextErrors[selectedCase.id]
          return nextErrors
        })

        try {
          const result = await uploadMultimodalCaseDocument(
            selectedCase.file,
            selectedCase.images.map((image) => image.file),
            (percentCompleted) => {
              setProgresses((currentProgresses) => ({
                ...currentProgresses,
                [selectedCase.id]: percentCompleted
              }))
            }
          )

          if (result.status !== 'success') {
            setCaseErrors((currentErrors) => ({
              ...currentErrors,
              [selectedCase.id]: result.message
            }))
            continue
          }

          successfulCaseIds.add(selectedCase.id)
          hasSuccessfulUpload = true
          if (!batchProbeTriggered) {
            batchProbeTriggered = true
            onUploadBatchAccepted?.()
          }
        } catch (error) {
          let message = errorMessage(error)
          if (error && typeof error === 'object' && 'response' in error) {
            const axiosError = error as {
              response?: { status: number; data?: { detail?: string } }
            }
            const status = axiosError.response?.status
            const detail = axiosError.response?.data?.detail
            if (status === 409 || status === 400 || status === 413) {
              message = detail || message
            }
          }

          setCaseErrors((currentErrors) => ({
            ...currentErrors,
            [selectedCase.id]: message
          }))
          setProgresses((currentProgresses) => ({
            ...currentProgresses,
            [selectedCase.id]: 100
          }))
        }
      }

      if (successfulCaseIds.size > 0) {
        setCases((currentCases) => {
          const remainingCases = currentCases.filter(
            (selectedCase) => !successfulCaseIds.has(selectedCase.id)
          )
          currentCases
            .filter((selectedCase) => successfulCaseIds.has(selectedCase.id))
            .forEach(revokeCasePreviews)
          setActiveCaseId((currentActiveCaseId) => {
            if (remainingCases.some((selectedCase) => selectedCase.id === currentActiveCaseId)) {
              return currentActiveCaseId
            }
            return remainingCases[0]?.id ?? null
          })
          return remainingCases
        })
        setProgresses((currentProgresses) =>
          Object.fromEntries(
            Object.entries(currentProgresses).filter(
              ([caseId]) => !successfulCaseIds.has(caseId)
            )
          )
        )
        setCaseErrors((currentErrors) =>
          Object.fromEntries(
            Object.entries(currentErrors).filter(
              ([caseId]) => !successfulCaseIds.has(caseId)
            )
          )
        )
      }

      if (hasSuccessfulUpload) {
        await onDocumentsUploaded?.()
      }

      if (successfulCaseIds.size && successfulCaseIds.size === sortedCases.length) {
        toast.success(t('documentPanel.uploadDocuments.batch.success'), { id: toastId })
        setOpen(false)
        resetDialogState()
      } else if (successfulCaseIds.size > 0) {
        toast.error(
          t('documentPanel.uploadDocuments.multimodalPartialFailure', {
            defaultValue: 'Some cases uploaded successfully, but others still need attention.'
          }),
          { id: toastId }
        )
      } else {
        toast.error(t('documentPanel.uploadDocuments.batch.error'), { id: toastId })
      }
    } catch (error) {
      toast.error(
        t('documentPanel.uploadDocuments.generalError', {
          error: errorMessage(error)
        }),
        { id: toastId }
      )
    } finally {
      setIsUploading(false)
    }
  }, [cases, onDocumentsUploaded, onUploadBatchAccepted, resetDialogState, t])

  const textCaseDescription = t('documentPanel.uploadDocuments.multimodalCaseFileTypes', {
    defaultValue:
      'Text case types: TXT, MD, TEXTPACK, MDX, HTML, HTM, CSV, JSON, XML, YAML, YML, LOG, CONF, INI, PROPERTIES, SQL, BAT, SH, C, H, CPP, HPP, PY, JAVA, JS, TS, SWIFT, GO, RB, PHP, CSS, SCSS, LESS, TEX, RTF'
  })
  const imageDescription = t('documentPanel.uploadDocuments.multimodalImageFileTypes', {
    defaultValue: 'Image types: PNG, JPG, JPEG, WEBP, GIF, BMP'
  })

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        <Button
          variant="default"
          side="bottom"
          tooltip={t('documentPanel.uploadDocuments.tooltip')}
          size="sm"
        >
          <UploadIcon /> {t('documentPanel.uploadDocuments.button')}
        </Button>
      </DialogTrigger>
      <DialogContent
        className="sm:max-w-5xl"
        onCloseAutoFocus={(event) => event.preventDefault()}
      >
        <DialogHeader>
          <DialogTitle>{t('documentPanel.uploadDocuments.title')}</DialogTitle>
          <DialogDescription>
            {t('documentPanel.uploadDocuments.multimodalDescription', {
              defaultValue:
                'Stage one text case at a time, attach up to {{count}} linked images to the active case, then upload each case as a single multimodal record.',
              count: maxCaseImages
            })}
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4 lg:grid-cols-[1.35fr_1fr]">
          <Card>
            <CardHeader className="pb-4">
              <CardTitle className="flex items-center gap-2 text-base">
                <FileText className="size-4" />
                {t('documentPanel.uploadDocuments.textCasesTitle', {
                  defaultValue: 'Text Cases'
                })}
              </CardTitle>
              <CardDescription>{textCaseDescription}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <Dropzone
                accept={multimodalTextCaseFileTypes}
                disabled={isUploading}
                multiple
                onDrop={addTextCases}
              >
                {({ getRootProps, getInputProps, isDragActive }) => (
                  <div
                    {...getRootProps()}
                    className={cn(
                      'rounded-xl border border-dashed p-6 text-center transition-colors cursor-pointer',
                      isDragActive ? 'border-primary bg-primary/5' : 'border-border hover:border-primary/60'
                    )}
                  >
                    <input {...getInputProps()} />
                    <p className="font-medium">
                      {t('documentPanel.uploadDocuments.textCasesDropzone', {
                        defaultValue:
                          'Drag text case files here, or click to browse.'
                      })}
                    </p>
                    <p className="text-muted-foreground mt-2 text-sm">
                      {t('documentPanel.uploadDocuments.textCasesHint', {
                        defaultValue:
                          'Each staged file becomes one case that can carry its own linked image set.'
                      })}
                    </p>
                  </div>
                )}
              </Dropzone>

              <ScrollArea className="max-h-[420px] pr-3">
                <div className="space-y-3">
                  {cases.length === 0 ? (
                    <div className="text-muted-foreground rounded-lg border border-dashed p-6 text-sm">
                      {t('documentPanel.uploadDocuments.noCasesStaged', {
                        defaultValue: 'No cases staged yet.'
                      })}
                    </div>
                  ) : (
                    cases.map((selectedCase) => {
                      const isActive = selectedCase.id === activeCaseId
                      const progress = progresses[selectedCase.id]
                      const error = caseErrors[selectedCase.id]

                      return (
                        <div
                          key={selectedCase.id}
                          className={cn(
                            'w-full rounded-xl border p-4 text-left transition-colors',
                            isActive ? 'border-primary bg-primary/5' : 'border-border hover:border-primary/50'
                          )}
                          onClick={() => setActiveCaseId(selectedCase.id)}
                          onKeyDown={(event) => {
                            if (event.key === 'Enter' || event.key === ' ') {
                              event.preventDefault()
                              setActiveCaseId(selectedCase.id)
                            }
                          }}
                          role="button"
                          tabIndex={0}
                        >
                          <div className="flex items-start justify-between gap-3">
                            <div className="min-w-0">
                              <div className="truncate font-medium">{selectedCase.file.name}</div>
                              <div className="text-muted-foreground mt-1 text-xs">
                                {t('documentPanel.uploadDocuments.caseImageCount', {
                                  defaultValue: '{{count}} image(s) attached',
                                  count: selectedCase.images.length
                                })}
                              </div>
                            </div>
                            <Button
                              size="icon"
                              type="button"
                              variant="ghost"
                              onClick={(event) => {
                                event.stopPropagation()
                                removeCase(selectedCase.id)
                              }}
                            >
                              <XIcon />
                            </Button>
                          </div>

                          {typeof progress === 'number' ? (
                            <div className="mt-3 space-y-2">
                              <Progress value={progress} />
                              <div className="text-muted-foreground text-xs">
                                {progress < 100
                                  ? t('documentPanel.uploadDocuments.single.uploading', {
                                    name: selectedCase.file.name,
                                    percent: progress
                                  })
                                  : t('documentPanel.uploadDocuments.uploadComplete', {
                                    defaultValue: 'Upload request completed.'
                                  })}
                              </div>
                            </div>
                          ) : null}

                          {error ? (
                            <div className="mt-3 text-sm text-red-500">{error}</div>
                          ) : null}
                        </div>
                      )
                    })
                  )}
                </div>
              </ScrollArea>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-4">
              <CardTitle className="flex items-center gap-2 text-base">
                <ImageIcon className="size-4" />
                {t('documentPanel.uploadDocuments.caseImagesTitle', {
                  defaultValue: 'Linked Images'
                })}
              </CardTitle>
              <CardDescription>
                {activeCase
                  ? t('documentPanel.uploadDocuments.caseImagesDescription', {
                    defaultValue: 'Attach up to {{count}} images to the active case: {{name}}',
                    count: maxCaseImages,
                    name: activeCase.file.name
                  })
                  : t('documentPanel.uploadDocuments.caseImagesEmptyState', {
                    defaultValue: 'Select a staged text case to attach images.'
                  })}
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <Dropzone
                accept={multimodalImageFileTypes}
                disabled={isUploading || !activeCase}
                multiple
                onDrop={addImagesToActiveCase}
              >
                {({ getRootProps, getInputProps, isDragActive }) => (
                  <div
                    {...getRootProps()}
                    className={cn(
                      'rounded-xl border border-dashed p-6 text-center transition-colors',
                      activeCase ? 'cursor-pointer' : 'cursor-not-allowed opacity-60',
                      isDragActive && activeCase ? 'border-primary bg-primary/5' : 'border-border hover:border-primary/60'
                    )}
                  >
                    <input {...getInputProps()} />
                    <p className="font-medium">
                      {t('documentPanel.uploadDocuments.imagesDropzone', {
                        defaultValue: 'Drag linked images here, or click to browse.'
                      })}
                    </p>
                    <p className="text-muted-foreground mt-2 text-sm">{imageDescription}</p>
                  </div>
                )}
              </Dropzone>

              <ScrollArea className="max-h-[420px] pr-3">
                <div className="space-y-3">
                  {activeCase?.images.length ? (
                    activeCase.images.map((image) => (
                      <div
                        key={image.id}
                        className="flex items-center gap-3 rounded-xl border p-3"
                      >
                        <img
                          alt={image.file.name}
                          className="h-16 w-16 rounded-lg object-cover"
                          src={image.previewUrl}
                        />
                        <div className="min-w-0 flex-1">
                          <div className="truncate text-sm font-medium">
                            {image.file.name}
                          </div>
                          <div className="text-muted-foreground text-xs">
                            {(image.file.size / 1024 / 1024).toFixed(2)} MB
                          </div>
                        </div>
                        <Button
                          size="icon"
                          type="button"
                          variant="ghost"
                          onClick={() => removeImageFromCase(activeCase.id, image.id)}
                        >
                          <XIcon />
                        </Button>
                      </div>
                    ))
                  ) : (
                    <div className="text-muted-foreground rounded-lg border border-dashed p-6 text-sm">
                      {t('documentPanel.uploadDocuments.noImagesAttached', {
                        defaultValue: 'No images attached to this case yet.'
                      })}
                    </div>
                  )}
                </div>
              </ScrollArea>
            </CardContent>
          </Card>
        </div>

        <div className="flex items-center justify-between gap-3">
          <div className="text-muted-foreground text-sm">
            {t('documentPanel.uploadDocuments.stageSummary', {
              defaultValue: '{{caseCount}} case(s) staged.',
              caseCount: cases.length
            })}
          </div>
          <div className="flex items-center gap-3">
            <Button
              disabled={isUploading}
              variant="outline"
              onClick={() => handleOpenChange(false)}
            >
              {t('common.cancel')}
            </Button>
            <Button
              disabled={isUploading || cases.length === 0}
              onClick={handleDocumentsUpload}
            >
              <UploadIcon />
              {t('documentPanel.uploadDocuments.uploadCasesButton', {
                defaultValue: 'Upload Staged Cases'
              })}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
