import {
  Component,
  EventEmitter,
  Input,
  Output,
  ViewChild,
} from '@angular/core'
import { NgbPopover } from '@ng-bootstrap/ng-bootstrap'
import { map } from 'rxjs/operators'
import { PaperlessStoragePath } from 'src/app/data/paperless-storage-path'
import { SETTINGS_KEYS } from 'src/app/data/paperless-uisettings'
import { StoragePathService } from 'src/app/services/rest/storage-path.service'
import { SettingsService } from 'src/app/services/settings.service'
import { ComponentWithPermissions } from '../../with-permissions/with-permissions.component'

@Component({
  selector: 'app-folder-card-small',
  templateUrl: './folder-card-small.component.html',
  styleUrls: [
    './folder-card-small.component.scss',
    '../popover-preview/popover-preview.scss',
  ],
})
export class FolderCardSmallComponent extends ComponentWithPermissions {
  constructor(
    private storagePathService: StoragePathService,
    private settingsService: SettingsService
  ) {
    super()
  }

  @Input()
  selected = false

  @Output()
  toggleSelected = new EventEmitter()

  @Input()
  storagePath: PaperlessStoragePath

  @Output()
  dblClickDocument = new EventEmitter()

  @Output()
  clickTag = new EventEmitter<number>()

  @Output()
  clickCorrespondent = new EventEmitter<number>()

  @Output()
  clickDocumentType = new EventEmitter<number>()

  @Output()
  clickStoragePath = new EventEmitter<number>()

  moreTags: number = null

  @ViewChild('popover') popover: NgbPopover

  mouseOnPreview = false
  popoverHidden = true

  getIsThumbInverted() {
    return this.settingsService.get(SETTINGS_KEYS.DARK_MODE_THUMB_INVERTED)
  }

  getThumbUrl() {
    return ''
  }

  getDownloadUrl() {
    return ''
  }

  get previewUrl() {
    return ''
  }

  mouseEnterPreview() {
    this.mouseOnPreview = true
    if (!this.popover.isOpen()) {
      // we're going to open but hide to pre-load content during hover delay
      this.popover.open()
      this.popoverHidden = true
      setTimeout(() => {
        if (this.mouseOnPreview) {
          // show popover
          this.popoverHidden = false
        } else {
          this.popover.close()
        }
      }, 600)
    }
  }

  mouseLeavePreview() {
    this.mouseOnPreview = false
  }

  mouseLeaveCard() {
    this.popover.close()
  }

  get notesEnabled(): boolean {
    return this.settingsService.get(SETTINGS_KEYS.NOTES_ENABLED)
  }
}
